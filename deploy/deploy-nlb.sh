#!/usr/bin/env bash
# ============================================================================
# DataVigil — deploy to ECS Fargate behind a public Network Load Balancer.
# Pure AWS CLI v2 — no console steps. Run from Git Bash / Linux / CloudShell.
#
# Required:
#   REGION=us-east-1                       (or aws configure default)
#   SUBNETS="subnet-aaa subnet-bbb"        subnets for BOTH tasks and the NLB
#
# Optional (sane defaults for testing):
#   PROJECT=datavigil  CLUSTER=datavigil   IMAGE_TAG=latest
#   CPU=1024  MEMORY=2048  DESIRED=1
#   ALLOWED_CIDR=0.0.0.0/0                 who may reach the NLB :8000
#   ASSIGN_PUBLIC_IP=ENABLED               tasks need it only for ECR pull
#                                          without a NAT gateway
#   EFS_ID=fs-xxxx                         persist /data state (optional)
#   LLM_PROVIDER=bedrock  LLM_MODEL=us.amazon.nova-pro-v1:0
#   EXECUTION_ROLE_ARN / TASK_ROLE_ARN     default: ecsTaskExecutionRole and
#                                          datavigil-task in this account
#   SKIP_BUILD=1                           reuse existing ECR image
#
# Example:
#   REGION=us-east-1 SUBNETS="subnet-0123 subnet-0456" bash deploy/deploy-nlb.sh
# ============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PROJECT="${PROJECT:-datavigil}"
CLUSTER="${CLUSTER:-datavigil}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
CPU="${CPU:-1024}"; MEMORY="${MEMORY:-2048}"
DESIRED="${DESIRED:-1}"
REGION="${REGION:-$(aws configure get region)}"
ALLOWED_CIDR="${ALLOWED_CIDR:-0.0.0.0/0}"
ASSIGN_PUBLIC_IP="${ASSIGN_PUBLIC_IP:-ENABLED}"
EFS_ID="${EFS_ID:-}"
LOG_GROUP="${LOG_GROUP:-/ecs/datavigil}"
LLM_PROVIDER="${LLM_PROVIDER:-bedrock}"
LLM_MODEL="${LLM_MODEL:-us.amazon.nova-pro-v1:0}"
NLB_NAME="${NLB_NAME:-datavigil-nlb}"
TG_NAME="${TG_NAME:-datavigil-tg}"
SG_NAME="${SG_NAME:-datavigil-task}"

: "${SUBNETS:?set SUBNETS='subnet-a subnet-b' (public subnets for the NLB)}"
read -ra SNS <<< "$SUBNETS"
SUBNET_CSV="$(IFS=,; echo "${SNS[*]}")"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$PROJECT"
IMAGE="$REPO:$IMAGE_TAG"
EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN:-arn:aws:iam::$ACCOUNT_ID:role/ecsTaskExecutionRole}"
TASK_ROLE_ARN="${TASK_ROLE_ARN:-arn:aws:iam::$ACCOUNT_ID:role/datavigil-task}"

VPC_ID="$(aws ec2 describe-subnets --subnet-ids "${SNS[0]}" --region "$REGION" \
          --query 'Subnets[0].VpcId' --output text)"
VPC_CIDR="$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$REGION" \
            --query 'Vpcs[0].CidrBlock' --output text)"
echo "==> account $ACCOUNT_ID · region $REGION · vpc $VPC_ID ($VPC_CIDR)"

# ---- 1. image ---------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  command -v docker >/dev/null || { echo "ERROR: docker not found — run this in AWS CloudShell or set SKIP_BUILD=1"; exit 1; }
  echo "==> ECR repo + build/push $IMAGE"
  aws ecr describe-repositories --repository-names "$PROJECT" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$PROJECT" --region "$REGION" >/dev/null
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
  docker build -t "$IMAGE" -f "$ROOT/deploy/Dockerfile" "$ROOT"
  docker push "$IMAGE"
fi

# ---- 2. log group + cluster --------------------------------------------------
echo "==> log group + cluster"
aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$REGION" \
  | grep -q "\"logGroupName\": \"$LOG_GROUP\"" \
  || aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$REGION" >/dev/null
aws ecs describe-clusters --clusters "$CLUSTER" --region "$REGION" \
  | grep -q '"status": "ACTIVE"' \
  || aws ecs create-cluster --cluster-name "$CLUSTER" --region "$REGION" >/dev/null
# ECS service-linked role — no-op if it already exists
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com 2>/dev/null || true

# ---- 3. task security group --------------------------------------------------
# NLB health checks arrive from the NLB ENIs (inside the VPC), client traffic
# arrives with the real client IP — so allow ALLOWED_CIDR + the VPC CIDR.
echo "==> security group $SG_NAME"
SG_ID="$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
  SG_ID="$(aws ec2 create-security-group --group-name "$SG_NAME" \
    --description "DataVigil ECS tasks" --vpc-id "$VPC_ID" --region "$REGION" \
    --query GroupId --output text)"
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --region "$REGION" \
    --protocol tcp --port 8000 --cidr "$ALLOWED_CIDR" >/dev/null
  [[ "$VPC_CIDR" != "$ALLOWED_CIDR" ]] \
    && aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --region "$REGION" \
         --protocol tcp --port 8000 --cidr "$VPC_CIDR" >/dev/null || true
fi
echo "    SG: $SG_ID  (8000 open to $ALLOWED_CIDR + $VPC_CIDR)"

# ---- 4. NLB + target group + listener ---------------------------------------
echo "==> NLB / target group / listener"
NLB_ARN="$(aws elbv2 describe-load-balancers --names "$NLB_NAME" --region "$REGION" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [[ -z "$NLB_ARN" || "$NLB_ARN" == "None" ]]; then
  NLB_ARN="$(aws elbv2 create-load-balancer --name "$NLB_NAME" --type network \
    --scheme internet-facing --subnets ${SNS[*]} --region "$REGION" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
fi
TG_ARN="$(aws elbv2 describe-target-groups --names "$TG_NAME" --region "$REGION" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
if [[ -z "$TG_ARN" || "$TG_ARN" == "None" ]]; then
  TG_ARN="$(aws elbv2 create-target-group --name "$TG_NAME" --protocol TCP \
    --port 8000 --target-type ip --vpc-id "$VPC_ID" --region "$REGION" \
    --health-check-protocol HTTP --health-check-path /api/health \
    --health-check-interval-seconds 30 --healthy-threshold-count 2 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)"
  aws elbv2 modify-target-group-attributes --target-group-arn "$TG_ARN" \
    --region "$REGION" \
    --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
fi
LST_ARN="$(aws elbv2 describe-listeners --load-balancer-arn "$NLB_ARN" \
  --region "$REGION" --query 'Listeners[0].ListenerArn' --output text 2>/dev/null || true)"
if [[ -z "$LST_ARN" || "$LST_ARN" == "None" ]]; then
  aws elbv2 create-listener --load-balancer-arn "$NLB_ARN" --protocol TCP \
    --port 8000 --region "$REGION" \
    --default-actions "Type=forward,TargetGroupArn=$TG_ARN" >/dev/null
fi

# ---- 5. task definition ------------------------------------------------------
echo "==> task definition"
PYBIN="$(command -v python3 || command -v python)"
render() { sed -e "s|{{EXECUTION_ROLE_ARN}}|$EXECUTION_ROLE_ARN|g" \
    -e "s|{{TASK_ROLE_ARN}}|$TASK_ROLE_ARN|g" -e "s|{{IMAGE_URI}}|$IMAGE|g" \
    -e "s|{{EFS_ID}}|$EFS_ID|g" -e "s|{{LOG_GROUP}}|$LOG_GROUP|g" \
    -e "s|{{LLM_PROVIDER}}|$LLM_PROVIDER|g" -e "s|{{LLM_MODEL}}|$LLM_MODEL|g" \
    -e "s|{{REGION}}|$REGION|g" "$1"; }
TMP_TD="$(mktemp --suffix=.json)"
render "$ROOT/deploy/ecs/taskdef.service.json" \
  | "$PYBIN" -c 'import json, sys
d = json.load(sys.stdin)
efs, cpu, mem = sys.argv[1:4]
d["cpu"], d["memory"] = cpu, mem
if not efs:
    d["volumes"] = []
    d["containerDefinitions"][0]["mountPoints"] = []
json.dump(d, sys.stdout)' "$EFS_ID" "$CPU" "$MEMORY" >"$TMP_TD"
TD_ARN="$(aws ecs register-task-definition --cli-input-json "file://$TMP_TD" \
  --region "$REGION" --query 'taskDefinition.taskDefinitionArn' --output text)"
rm -f "$TMP_TD"; echo "    $TD_ARN"

# ---- 6. service --------------------------------------------------------------
echo "==> ECS service"
NET="awsvpcConfiguration={subnets=[$SUBNET_CSV],securityGroups=[$SG_ID],assignPublicIp=$ASSIGN_PUBLIC_IP}"
if aws ecs describe-services --cluster "$CLUSTER" --services "$PROJECT" \
     --region "$REGION" | grep -q '"status": "ACTIVE"'; then
  aws ecs update-service --cluster "$CLUSTER" --service "$PROJECT" \
    --task-definition "$TD_ARN" --desired-count "$DESIRED" \
    --force-new-deployment --region "$REGION" >/dev/null
  echo "    service updated"
else
  aws ecs create-service --cluster "$CLUSTER" --service-name "$PROJECT" \
    --task-definition "$TD_ARN" --desired-count "$DESIRED" \
    --launch-type FARGATE --region "$REGION" \
    --network-configuration "$NET" \
    --load-balancers "targetGroupArn=$TG_ARN,containerName=datavigil,containerPort=8000" \
    --health-check-grace-period-seconds 60 >/dev/null
  echo "    service created"
fi

# ---- 7. wait + endpoint -------------------------------------------------------
echo "==> waiting for the NLB to become active…"
aws elbv2 wait load-balancer-available --load-balancer-arns "$NLB_ARN" --region "$REGION"
DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$NLB_ARN" \
      --region "$REGION" --query 'LoadBalancers[0].DNSName' --output text)"
cat <<EOF

============================================================
 DataVigil deployed.
   NLB endpoint : http://$DNS
   Health check : http://$DNS/api/health
   Logs         : aws logs tail $LOG_GROUP --follow --region $REGION
============================================================
Targets take 1-2 min to register healthy. If the page won't load yet:
  aws elbv2 describe-target-health --target-group-arn $TG_ARN --region $REGION
EOF

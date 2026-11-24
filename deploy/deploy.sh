#!/usr/bin/env bash
# DataVigil deploy — bash variant (AWS CloudShell / Linux / macOS).
# Same parameters as deploy.ps1, via env vars:
#
#   MODE=service|batch|both   REGION=us-east-1   CLUSTER=datavigil
#   SUBNETS="subnet-aaa subnet-bbb"   SECURITY_GROUPS="sg-xxx"
#   EFS_ID=fs-xxxx (optional)   EXECUTION_ROLE_ARN=...  TASK_ROLE_ARN=...
#   ASSIGN_PUBLIC_IP=ENABLED|DISABLED   LLM_PROVIDER=bedrock  LLM_MODEL=...
#   MANIFEST_PATH=/data/batch/manifest.yml   SCHEDULE="cron(0 2 * * ? *)"
#   SCHEDULER_ROLE_ARN=...   SKIP_BUILD=1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

MODE="${MODE:-service}"
PROJECT="${PROJECT:-datavigil}"
CLUSTER="${CLUSTER:-datavigil}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
CPU="${CPU:-1024}"; MEMORY="${MEMORY:-2048}"
REGION="${REGION:-$(aws configure get region)}"
LOG_GROUP="${LOG_GROUP:-/ecs/datavigil}"
MANIFEST_PATH="${MANIFEST_PATH:-/data/batch/manifest.yml}"
LLM_PROVIDER="${LLM_PROVIDER:-bedrock}"
LLM_MODEL="${LLM_MODEL:-us.amazon.nova-pro-v1:0}"
ASSIGN_PUBLIC_IP="${ASSIGN_PUBLIC_IP:-ENABLED}"
DESIRED="${DESIRED:-1}"
ALB_TG_ARN="${ALB_TG_ARN:-}"
EFS_ID="${EFS_ID:-}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$PROJECT"
IMAGE="$REPO:$IMAGE_TAG"
echo "Account $ACCOUNT_ID · region $REGION · mode $MODE"

: "${SUBNETS:?set SUBNETS='subnet-a subnet-b'}"
: "${SECURITY_GROUPS:?set SECURITY_GROUPS='sg-xxx'}"
read -ra SNS <<< "$SUBNETS"; read -ra SGS <<< "$SECURITY_GROUPS"
SUBNET_CSV="$(IFS=,; echo "${SNS[*]}")"; SG_CSV="$(IFS=,; echo "${SGS[*]}")"

render() {  # render template $1 -> stdout, substituting {{VARS}}
  sed -e "s|{{ACCOUNT_ID}}|$ACCOUNT_ID|g" -e "s|{{REGION}}|$REGION|g" \
      -e "s|{{IMAGE_URI}}|$IMAGE|g" -e "s|{{CLUSTER}}|$CLUSTER|g" \
      -e "s|{{EXECUTION_ROLE_ARN}}|${EXECUTION_ROLE_ARN:-arn:aws:iam::$ACCOUNT_ID:role/ecsTaskExecutionRole}|g" \
      -e "s|{{TASK_ROLE_ARN}}|${TASK_ROLE_ARN:-arn:aws:iam::$ACCOUNT_ID:role/datavigil-task}|g" \
      -e "s|{{EFS_ID}}|$EFS_ID|g" -e "s|{{LOG_GROUP}}|$LOG_GROUP|g" \
      -e "s|{{LLM_PROVIDER}}|$LLM_PROVIDER|g" -e "s|{{LLM_MODEL}}|$LLM_MODEL|g" \
      -e "s|{{MANIFEST_CONTAINER_PATH}}|$MANIFEST_PATH|g" \
      -e "s|{{SUBNET_1}}|${SNS[0]}|g" -e "s|{{SUBNET_2}}|${SNS[-1]}|g" \
      -e "s|{{SECURITY_GROUP}}|${SGS[0]}|g" "$1"
}

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  command -v docker >/dev/null || { echo "docker not found — run in CloudShell"; exit 1; }
  aws ecr describe-repositories --repository-names "$PROJECT" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$PROJECT" --region "$REGION" >/dev/null
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
  docker build -t "$IMAGE" -f "$ROOT/deploy/Dockerfile" "$ROOT"
  docker push "$IMAGE"
fi

aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$REGION" \
  | grep -q "\"logGroupName\": \"$LOG_GROUP\"" \
  || aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$REGION" >/dev/null

aws ecs describe-clusters --clusters "$CLUSTER" --region "$REGION" \
  | grep -q '"status": "ACTIVE"' \
  || aws ecs create-cluster --cluster-name "$CLUSTER" --region "$REGION" >/dev/null

register_td() { # $1 = template file
  local tmp; tmp="$(mktemp --suffix=.json)"
  render "$ROOT/deploy/ecs/$1" \
    | python3 -c 'import json,sys
d=json.load(sys.stdin)
efs,cpu,mem=sys.argv[1:4]
d["cpu"]=cpu; d["memory"]=mem
if not efs:
    d["volumes"]=[]; d["containerDefinitions"][0]["mountPoints"]=[]
json.dump(d,sys.stdout)' "$EFS_ID" "$CPU" "$MEMORY" >"$tmp"
  aws ecs register-task-definition --cli-input-json "file://$tmp" --region "$REGION" \
    --query 'taskDefinition.taskDefinitionArn' --output text
  rm -f "$tmp"
}

NET="awsvpcConfiguration={subnets=[$SUBNET_CSV],securityGroups=[$SG_CSV],assignPublicIp=$ASSIGN_PUBLIC_IP}"

if [[ "$MODE" == "service" || "$MODE" == "both" ]]; then
  TD="$(register_td taskdef.service.json)"; echo "taskdef: $TD"
  if aws ecs describe-services --cluster "$CLUSTER" --services "$PROJECT" --region "$REGION" \
       | grep -q '"status": "ACTIVE"'; then
    aws ecs update-service --cluster "$CLUSTER" --service "$PROJECT" \
      --task-definition "$TD" --desired-count "$DESIRED" \
      --force-new-deployment --region "$REGION" >/dev/null
    echo "service updated"
  else
    LB=(); [[ -n "$ALB_TG_ARN" ]] && LB=(--load-balancers "targetGroupArn=$ALB_TG_ARN,containerName=datavigil,containerPort=8000")
    aws ecs create-service --cluster "$CLUSTER" --service-name "$PROJECT" \
      --task-definition "$TD" --desired-count "$DESIRED" --launch-type FARGATE \
      --network-configuration "$NET" --enable-execute-command \
      --region "$REGION" "${LB[@]}" >/dev/null
    echo "service created — http://<task-ip>:8000"
  fi
fi

if [[ "$MODE" == "batch" || "$MODE" == "both" ]]; then
  TD="$(register_td taskdef.batch.json)"; echo "taskdef: $TD"
  echo "Run once: aws ecs run-task --cluster $CLUSTER --launch-type FARGATE --task-definition $TD --network-configuration \"$NET\""
  if [[ -n "${SCHEDULE:-}" ]]; then
    tmp="$(mktemp --suffix=.json)"
    render "$ROOT/deploy/eventbridge/schedule.example.json" \
      | sed -e "s|{{TASKDEF_ARN}}|$TD|g" \
            -e "s|{{SCHEDULER_ROLE_ARN}}|${SCHEDULER_ROLE_ARN:-arn:aws:iam::$ACCOUNT_ID:role/datavigil-scheduler}|g" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); d['ScheduleExpression']='$SCHEDULE'; json.dump(d,sys.stdout)" >"$tmp"
    aws scheduler create-schedule --cli-input-json "file://$tmp" --region "$REGION"
    rm -f "$tmp"
    echo "schedule created"
  fi
fi
echo "Done."

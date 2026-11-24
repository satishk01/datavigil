# DataVigil — ECS Deployment Runbook

Deploy DataVigil as a simple always-on Fargate service — no EventBridge, no
scheduler. Two ways to reach it: **A) the task's public IP** (simplest) or
**B) an NLB endpoint** (stable DNS). Pick one in section 7.

> Everything below is AWS CLI v2. Run in AWS CloudShell (recommended — CLI,
> Docker and credentials are built in) or any shell with `aws` + `docker`
> configured.

---

## 0. Prerequisites

```bash
aws sts get-caller-identity     # must print your account id — if not: aws configure
docker version                  # must show Client + Server — else use CloudShell
```

## 1. Set variables — EDIT THIS BLOCK ONLY

```bash
REGION=us-east-1
PROJECT=datavigil
CLUSTER=datavigil

# Find a public subnet + VPC. Default VPC subnets work fine for testing:
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text --region $REGION)
SUBNET_1=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC \
      --query 'Subnets[0].SubnetId' --output text --region $REGION)
SUBNET_2=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC \
      --query 'Subnets[1].SubnetId' --output text --region $REGION)
echo "VPC=$VPC  SUBNET_1=$SUBNET_1  SUBNET_2=$SUBNET_2"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
IMAGE=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$PROJECT:latest
LOG_GROUP=/ecs/datavigil
```

> If the subnet lookups return `None`, pick subnet ids from the VPC console
> and set `SUBNET_1`/`SUBNET_2` manually.

## 2. Build the image and push to ECR

```bash
aws ecr create-repository --repository-name $PROJECT --region $REGION \
  2>/dev/null || true   # ok if it already exists

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin \
      $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

# run from the repo root
docker build -t $IMAGE -f deploy/Dockerfile .
docker push $IMAGE
```

## 3. Cluster + log group

```bash
aws ecs create-cluster --cluster-name $CLUSTER --region $REGION 2>/dev/null || true
aws logs create-log-group --log-group-name $LOG_GROUP --region $REGION 2>/dev/null || true

# ECS service-linked role — no-op if it already exists
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com 2>/dev/null || true
```

## 4. IAM roles — one-time setup

```bash
# execution role (pulls image + writes logs) — skip create-role if it exists
aws iam create-role --role-name ecsTaskExecutionRole \
  --assume-role-policy-document file://deploy/iam/ecs-task-trust.json 2>/dev/null || true
aws iam attach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# task role (what the app itself may call — Bedrock, RDS, Secrets Manager)
aws iam create-role --role-name datavigil-task \
  --assume-role-policy-document file://deploy/iam/ecs-task-trust.json 2>/dev/null || true
aws iam put-role-policy --role-name datavigil-task \
  --policy-name datavigil-task \
  --policy-document file://deploy/iam/datavigil-task-policy.json
```

## 5. Security group for the tasks

Opens port 8000 to the internet. To restrict it, replace `0.0.0.0/0` with
your IP: `$(curl -s ifconfig.me)/32`. The VPC CIDR ingress is required if you
use the NLB option (health checks come from inside the VPC).

```bash
VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids $VPC \
  --query 'Vpcs[0].CidrBlock' --output text --region $REGION)

SG=$(aws ec2 create-security-group --group-name datavigil-task \
     --description "DataVigil ECS tasks" --vpc-id $VPC \
     --query GroupId --output text --region $REGION)

aws ec2 authorize-security-group-ingress --group-id $SG \
  --protocol tcp --port 8000 --cidr 0.0.0.0/0 --region $REGION
aws ec2 authorize-security-group-ingress --group-id $SG \
  --protocol tcp --port 8000 --cidr $VPC_CIDR --region $REGION

echo "SG=$SG"
```

## 6. Register the task definition

Inline JSON — no templates needed. `DATAVIGIL_LLM_PROVIDER=none` keeps AI
features off; set it to `bedrock` and a model to enable them.

```bash
cat > /tmp/datavigil-td.json <<EOF
{
  "family": "$PROJECT",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "arn:aws:iam::$ACCOUNT:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::$ACCOUNT:role/datavigil-task",
  "containerDefinitions": [{
    "name": "datavigil",
    "image": "$IMAGE",
    "essential": true,
    "portMappings": [{ "containerPort": 8000, "protocol": "tcp" }],
    "environment": [
      { "name": "DATAVIGIL_HOME", "value": "/data" },
      { "name": "DATAVIGIL_LLM_PROVIDER", "value": "none" },
      { "name": "AWS_REGION", "value": "$REGION" }
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "$LOG_GROUP",
        "awslogs-region": "$REGION",
        "awslogs-stream-prefix": "service"
      }
    }
  }]
}
EOF

aws ecs register-task-definition \
  --cli-input-json file:///tmp/datavigil-td.json --region $REGION \
  --query 'taskDefinition.taskDefinitionArn' --output text
```

> State is ephemeral (sources/mappings/baselines live in `/data`). For
> persistence add an EFS volume to the task def — see deploy/README.md.

## 7a. OPTION A — service with a public IP (simplest)

```bash
aws ecs create-service --cluster $CLUSTER --service-name $PROJECT \
  --task-definition $PROJECT --desired-count 1 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --region $REGION

# wait ~1-2 min for the task to start, then get its public IP:
TASK=$(aws ecs list-tasks --cluster $CLUSTER --service-name $PROJECT \
       --query 'taskArns[0]' --output text --region $REGION)
ENI=$(aws ecs describe-tasks --cluster $CLUSTER --tasks $TASK --region $REGION \
      --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text)
IP=$(aws ec2 describe-network-interfaces --network-interface-ids $ENI \
     --query 'NetworkInterfaces[0].Association.PublicIp' --output text --region $REGION)

echo "Open:  http://$IP:8000"
```

The IP changes if the task restarts — for a fixed endpoint use option B.

## 7b. OPTION B — service behind an NLB (stable DNS endpoint)

```bash
# load balancer + target group + listener
NLB_ARN=$(aws elbv2 create-load-balancer --name datavigil-nlb --type network \
  --scheme internet-facing --subnets $SUBNET_1 $SUBNET_2 --region $REGION \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

TG_ARN=$(aws elbv2 create-target-group --name datavigil-tg \
  --protocol TCP --port 8000 --target-type ip --vpc-id $VPC --region $REGION \
  --health-check-protocol HTTP --health-check-path /api/health \
  --health-check-interval-seconds 30 --healthy-threshold-count 2 \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

aws elbv2 create-listener --load-balancer-arn $NLB_ARN --protocol TCP \
  --port 8000 --region $REGION \
  --default-actions "Type=forward,TargetGroupArn=$TG_ARN"

# service attached to the target group
aws ecs create-service --cluster $CLUSTER --service-name $PROJECT \
  --task-definition $PROJECT --desired-count 1 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --load-balancers "targetGroupArn=$TG_ARN,containerName=datavigil,containerPort=8000" \
  --health-check-grace-period-seconds 60 --region $REGION

# wait for the NLB, print the endpoint
aws elbv2 wait load-balancer-available --load-balancer-arns $NLB_ARN --region $REGION
DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns $NLB_ARN \
      --query 'LoadBalancers[0].DNSName' --output text --region $REGION)

echo "Open:  http://$DNS        (health: http://$DNS/api/health)"
```

Targets take 1–2 min to register healthy:

```bash
aws elbv2 describe-target-health --target-group-arn $TG_ARN --region $REGION
```

## 8. Update / redeploy after code changes

```bash
docker build -t $IMAGE -f deploy/Dockerfile . && docker push $IMAGE
aws ecs update-service --cluster $CLUSTER --service $PROJECT \
  --force-new-deployment --region $REGION
```

## 9. Manual batch run (optional — no scheduler needed)

Put a manifest on an EFS share or bake it into the image (the image already
contains `/app/samples/batch_manifest.yml`), then:

```bash
aws ecs run-task --cluster $CLUSTER --launch-type FARGATE \
  --task-definition $PROJECT --region $REGION \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"datavigil","command":["batch","/app/samples/batch_manifest.yml"]}]}'
```

## 10. Teardown — remove everything

```bash
aws ecs update-service --cluster $CLUSTER --service $PROJECT --desired-count 0 --region $REGION
aws ecs delete-service --cluster $CLUSTER --service $PROJECT --force --region $REGION

# option B only:
aws elbv2 delete-listener --listener-arn $(aws elbv2 describe-listeners \
  --load-balancer-arn $NLB_ARN --query 'Listeners[0].ListenerArn' --output text --region $REGION) --region $REGION
aws elbv2 delete-load-balancer --load-balancer-arn $NLB_ARN --region $REGION
aws elbv2 delete-target-group --target-group-arn $TG_ARN --region $REGION

aws ec2 delete-security-group --group-id $SG --region $REGION   # after ENIs detach (~1 min)
aws ecs delete-cluster --cluster $CLUSTER --region $REGION
aws logs delete-log-group --log-group-name $LOG_GROUP --region $REGION
aws ecr delete-repository --repository-name $PROJECT --force --region $REGION
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Unable to locate credentials` | `aws configure` (or run in CloudShell) |
| Task keeps stopping | `aws logs tail $LOG_GROUP --follow --region $REGION` — usually image pull or a missing role |
| `ecsTaskExecutionRole` not found | Section 4 creates it |
| Can't pull image / task stuck PENDING | Subnet needs internet: public subnet + `assignPublicIp=ENABLED`, or a NAT gateway |
| NLB targets stay `unhealthy` | Check SG allows 8000 from the VPC CIDR (section 5), and `/api/health` responds in logs |
| Page won't load on public IP | Confirm SG allows 8000 from **your** IP — your IP may have changed; re-run the authorize command with `$(curl -s ifconfig.me)/32` |

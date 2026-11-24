# Deploying DataVigil

> **Just want the commands?** [`ecs-deploy.md`](../ecs-deploy.md) at the repo
> root is a copy-paste runbook — public-IP or NLB service, no EventBridge,
> with teardown steps.

## 0 — Local Docker (easiest, no AWS)

The image is self-contained — the `samples/` test pack is baked in, so the
whole black-box test plan (`test-steps.pdf`) runs inside the container.

```bash
# build + run, from the repo root
docker build -t datavigil -f deploy/Dockerfile .
docker run --rm -p 8000:8000 datavigil
# open http://localhost:8000
```

Or a single command with compose (Docker Desktop / docker-compose):

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Useful variants:

```bash
# keep app state (sources, mappings, baselines) between runs
docker run --rm -p 8000:8000 -v datavigil-state:/data datavigil

# run the sample batch manifest instead of the dashboard
docker run --rm datavigil batch /app/samples/batch_manifest.yml

# inside the container the samples live at /app/samples — register them via
# the "local file / copybook" form, e.g. path samples/parts_inventory.csv
```

## AWS — two deployment shapes, one image

| Shape | What runs | Trigger |
|-------|-----------|---------|
| **Service** | `datavigil serve` — dashboard + REST API on :8000 | always-on Fargate service behind an **NLB** (`deploy-nlb.sh`) or an ALB (`deploy.sh`) |
| **Batch** | `datavigil batch /data/batch/manifest.yml` | EventBridge Scheduler → ECS RunTask, or manual `aws ecs run-task` |

State (registered sources, rulesets, **mappings**, **schema baselines**, run
history, reports, uploads) lives in `DATAVIGIL_HOME=/data`. Mount an **EFS
filesystem** there or state is lost when a task stops. The task defs mount EFS
automatically when `-EfsId` / `EFS_ID` is provided.

## Prereqs

- AWS CLI v2, configured (`aws sts get-caller-identity` works)
- Docker daemon (local Docker Desktop **or** run `deploy.sh` in AWS CloudShell)
- An ECS cluster (script creates `datavigil` if missing)
- Subnets + a security group for the tasks' ENIs
- Recommended: an EFS filesystem + mount target in those subnets
  (NFS port 2049 open between the task SG and the EFS mount target SG)

## 1 — IAM roles

**Execution role** — pull from ECR + write logs. The AWS-managed
`ecsTaskExecutionRole` works as-is (script uses it by default). If your
account doesn't have it yet:

```bash
aws iam create-role --role-name ecsTaskExecutionRole \
  --assume-role-policy-document file://deploy/iam/ecs-task-trust.json
aws iam attach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
```

**Task role** (`datavigil-task` by default) — what the app itself may call.
Create it with the ready-made policy in `deploy/iam/`:

```bash
aws iam create-role --role-name datavigil-task \
  --assume-role-policy-document file://deploy/iam/ecs-task-trust.json
aws iam put-role-policy --role-name datavigil-task \
  --policy-name datavigil-task \
  --policy-document file://deploy/iam/datavigil-task-policy.json
```

The policy grants — remove the statements you don't need:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Bedrock",     "Effect": "Allow",
      "Action": "bedrock:InvokeModel", "Resource": "*" },
    { "Sid": "RdsIam",      "Effect": "Allow",
      "Action": "rds-db:connect", "Resource": "arn:aws:rds-db:*:*:dbuser:*/*" },
    { "Sid": "DataApi",     "Effect": "Allow",
      "Action": ["rds-data:ExecuteStatement","rds-data:BatchExecuteStatement"],
      "Resource": "arn:aws:rds:*:*:cluster:*" },
    { "Sid": "Secrets",     "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:*:*:secret:*" },
    { "Sid": "EfsWrite",    "Effect": "Allow",
      "Action": ["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite"],
      "Resource": "*" }
  ]
}
```

**Scheduler role** (batch mode only) — lets EventBridge run the task:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecs:RunTask",
      "Resource": "arn:aws:ecs:REGION:ACCOUNT:task-definition/datavigil-batch:*" },
    { "Effect": "Allow", "Action": "iam:PassRole",
      "Resource": ["arn:aws:iam::ACCOUNT:role/ecsTaskExecutionRole",
                   "arn:aws:iam::ACCOUNT:role/datavigil-task"] }
  ]
}
```
Trust policy: `"Principal": {"Service": "scheduler.amazonaws.com"}`.

## 2 — Deploy

### NLB service (recommended for a quick public endpoint)

`deploy/deploy-nlb.sh` is a single AWS CLI script that does **everything** —
ECR repo + image push, security group, internet-facing **NLB** + target group
(`ip` targets, HTTP health check on `/api/health`) + TCP:8000 listener, task
definition, Fargate service — and prints the endpoint URL at the end.
It is idempotent: re-running updates the image and service in place.

```bash
# Git Bash / Linux / AWS CloudShell — only subnets are required:
REGION=us-east-1 SUBNETS="subnet-aaa subnet-bbb" bash deploy/deploy-nlb.sh

# restricted access, persistent state on EFS:
REGION=us-east-1 SUBNETS="subnet-aaa subnet-bbb" \
  ALLOWED_CIDR="203.0.113.0/24" EFS_ID=fs-0123456789abcdef0 \
  bash deploy/deploy-nlb.sh
```

Notes:

- The script auto-creates a `datavigil-task` security group opening **8000**
  to `ALLOWED_CIDR` **and** the VPC CIDR — the NLB health checks arrive from
  inside the VPC, so both are needed.
- `ASSIGN_PUBLIC_IP=ENABLED` is the default so Fargate can pull from ECR
  without a NAT gateway; set it `DISABLED` if your subnets have NAT.
- The default roles are `ecsTaskExecutionRole` and `datavigil-task` (see
  IAM section). Override with `EXECUTION_ROLE_ARN` / `TASK_ROLE_ARN`.
- NLB is TCP-only — fine for HTTP on port 8000. If you need HTTPS/host-based
  routing later, swap in an ALB target group and pass `-AlbTargetGroupArn` /
  `ALB_TG_ARN` to `deploy.sh`.

### deploy.sh / deploy.ps1 (ALB-optional, batch + EventBridge)

Windows / PowerShell:

```powershell
.\deploy\deploy.ps1 -Mode service `
  -Region us-east-1 -Subnets subnet-aaa,subnet-bbb -SecurityGroups sg-xxxx `
  -EfsId fs-0123456789abcdef0 -AssignPublicIp ENABLED

# scheduled batch, nightly 02:00 UTC
.\deploy\deploy.ps1 -Mode batch `
  -Subnets subnet-aaa,subnet-bbb -SecurityGroups sg-xxxx `
  -EfsId fs-0123456789abcdef0 -ScheduleExpression "cron(0 2 * * ? *)"
```

CloudShell / bash:

```bash
MODE=service REGION=us-east-1 \
  SUBNETS="subnet-aaa subnet-bbb" SECURITY_GROUPS="sg-xxxx" \
  EFS_ID=fs-0123456789abcdef0 bash deploy/deploy.sh
```

The script is idempotent: re-running builds a new image, registers a new
task-def revision, and updates the service in place.

## 3 — After deploy (service mode)

- Public subnet + `AssignPublicIp ENABLED` → open `http://<task-public-ip>:8000`.
  Find it: `aws ecs list-tasks --cluster datavigil` → `describe-tasks` → ENI IP.
- Production: put it behind an **ALB** — create a target group (target type
  `ip`, port 8000, health check `/api/health`) and pass `-AlbTargetGroupArn`.
- Security group needs inbound 8000 from your network / the ALB SG only.
- LLM: default task env sets `DATAVIGIL_LLM_PROVIDER=bedrock` — enable the
  model in Bedrock and keep `bedrock:InvokeModel` on the task role. Or set
  `DATAVIGIL_LLM_PROVIDER=none` in the task def to disable AI features.

## 4 — Batch mode

The task runs `datavigil batch MANIFEST`. Put files + the manifest on the EFS
share (e.g. `/data/batch/manifest.yml` — see `deploy/manifest.example.yml`),
or copy them into the image / pull from S3 via an entry script.

```bash
# one-off run
aws ecs run-task --cluster datavigil --launch-type FARGATE \
  --task-definition datavigil-batch \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-aaa],securityGroups=[sg-xxxx],assignPublicIp=DISABLED}"

# schedule it (done automatically when -ScheduleExpression / SCHEDULE is set)
aws scheduler create-schedule --cli-input-json file://deploy/eventbridge/schedule.example.json
```

Batch exit code: the task exits non-zero when any job fails — visible in the
stopped-task status and CloudWatch logs; wire alarms on that.

## 5 — Reports to S3 (optional)

Reports land in `/data/reports` on EFS. To also push to S3, give the task role
`s3:PutObject` on your bucket and change the batch command to:

```json
"command": ["sh","-c","datavigil batch /data/batch/manifest.yml; rc=$?; aws s3 sync /data/reports s3://YOUR-BUCKET/datavigil-reports --region REGION; exit $rc"]
```
(the image would need `pip install awscli` added — or upload EFS backups via
AWS Backup instead.)

## Notes

- The Dockerfile builds with extras `postgres,mysql,aurora,bedrock,mcp,reports,snowflake`.
  SQL Server/Oracle drivers need OS packages (`unixodbc-dev`) — see the
  Dockerfile comment before enabling them.
- `aurora` IAM auth works on Fargate as long as the task SG can reach the
  cluster on 5432 and the task role has `rds-db:connect`.
- No S3 static hosting: the dashboard is a live FastAPI app — it needs the
  running container (EC2 is also fine: `pip install -e .[all]` + `datavigil serve`).

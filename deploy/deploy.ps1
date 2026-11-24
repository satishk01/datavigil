<#
.SYNOPSIS
  Deploy DataVigil to AWS: build + push image to ECR, register ECS task
  definition(s), create/update the Fargate service and/or a scheduled
  batch task.

.DESCRIPTION
  Run from the repository root:

    .\deploy\deploy.ps1 -Mode service -EfsId fs-0123456789abcdef0 `
        -Subnets subnet-aaa,subnet-bbb -SecurityGroups sg-xxxx

    .\deploy\deploy.ps1 -Mode batch -EfsId fs-0123456789abcdef0 `
        -Subnets subnet-aaa,subnet-bbb -SecurityGroups sg-xxxx `
        -ScheduleExpression "cron(0 2 * * ? *)"

  -Mode service : long-running dashboard (datavigil serve on :8000)
  -Mode batch   : scheduled ECS RunTask executing `datavigil batch`
  -Mode both    : both of the above

  Prerequisites: AWS CLI configured, Docker daemon running (or run the
  build steps in AWS CloudShell), an ECS cluster, and — recommended — an
  EFS filesystem so rulesets/mappings/schema baselines/reports persist
  across task restarts. Without -EfsId the state is ephemeral.
#>
[CmdletBinding()]
param(
  [ValidateSet('service','batch','both')] [string]$Mode = 'service',

  [string]$Region           = $env:AWS_REGION,
  [string]$Project          = 'datavigil',
  [string]$Cluster          = 'datavigil',
  [string]$ImageTag         = 'latest',
  [int]$Cpu                 = 1024,
  [int]$Memory              = 2048,

  # networking (required for awsvpc/Fargate)
  [string[]]$Subnets        = @(),
  [string[]]$SecurityGroups = @(),
  [ValidateSet('ENABLED','DISABLED')] [string]$AssignPublicIp = 'ENABLED',

  # persistence / iam
  [string]$EfsId            = '',
  [string]$ExecutionRoleArn = '',          # defaults: ecsTaskExecutionRole
  [string]$TaskRoleArn      = '',
  [string]$LogGroup         = '/ecs/datavigil',

  # service options
  [int]$DesiredCount        = 1,
  [string]$AlbTargetGroupArn = '',          # optional — attach behind an ALB
  [string]$LlmProvider      = 'bedrock',
  [string]$LlmModel         = 'us.amazon.nova-pro-v1:0',

  # batch options
  [string]$ManifestPath     = '/data/batch/manifest.yml',
  [string]$ScheduleExpression = '',         # e.g. "cron(0 2 * * ? *)" — empty = no schedule
  [string]$SchedulerRoleArn = '',

  [switch]$SkipBuild                          # reuse the last pushed image
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Render-Template([string]$file, [hashtable]$vars) {
  $json = Get-Content $file -Raw
  foreach ($k in $vars.Keys) { $json = $json.Replace("{{${k}}}", [string]$vars[$k]) }
  if ($json -match '\{\{[A-Z_]+\}\}') {
    throw "Unresolved placeholder(s) in $file : $($Matches[0])"
  }
  return ($json | ConvertFrom-Json)   # also validates JSON
}

# ---------- prereqs ---------------------------------------------------------
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'AWS CLI not found on PATH' }
$Region = if ($Region) { $Region } else { (aws configure get region) }
if (-not $Region) { throw 'Region not set — pass -Region or set AWS_REGION' }
$AccountId = (aws sts get-caller-identity --query Account --output text)
Write-Host "Account $AccountId · region $Region · mode $Mode"

if ($Subnets.Count -eq 0)        { throw '-Subnets is required (awsvpc networking)' }
if ($SecurityGroups.Count -eq 0) { throw '-SecurityGroups is required' }

$repo = "$AccountId.dkr.ecr.$Region.amazonaws.com/$Project"
$image = "${repo}:${ImageTag}"

# ---------- ECR + image -------------------------------------------------------
if (-not $SkipBuild) {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker not found. Install Docker Desktop or run this script in AWS CloudShell (docker is preinstalled there).'
  }
  aws ecr describe-repositories --repository-names $Project --region $Region 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating ECR repo $Project"
    aws ecr create-repository --repository-name $Project --region $Region | Out-Null
  }
  Write-Host 'Logging in to ECR…'
  aws ecr get-login-password --region $Region |
    docker login --username AWS --password-stdin "$AccountId.dkr.ecr.$Region.amazonaws.com" | Out-Null
  Write-Host "Building $image"
  docker build -t $image -f "$root\deploy\Dockerfile" $root
  docker push $image
} else {
  Write-Host "Skipping build — using $image"
}

# ---------- IAM roles ---------------------------------------------------------
if (-not $ExecutionRoleArn) {
  $ExecutionRoleArn = "arn:aws:iam::${AccountId}:role/ecsTaskExecutionRole"
  Write-Host "Using default execution role $ExecutionRoleArn"
}
if (-not $TaskRoleArn) {
  $TaskRoleArn = "arn:aws:iam::${AccountId}:role/datavigil-task"
  Write-Warning "No -TaskRoleArn given; assuming $TaskRoleArn exists (see deploy/README.md for the policy it needs)."
}

# ---------- log group ---------------------------------------------------------
$lg = aws logs describe-log-groups --log-group-name-prefix $LogGroup `
      --region $Region | ConvertFrom-Json
if (-not ($lg.logGroups | Where-Object { $_.logGroupName -eq $LogGroup })) {
  Write-Host "Creating log group $LogGroup"
  aws logs create-log-group --log-group-name $LogGroup --region $Region | Out-Null
}

# ---------- cluster -----------------------------------------------------------
$cl = aws ecs describe-clusters --clusters $Cluster --region $Region | ConvertFrom-Json
if (-not ($cl.clusters | Where-Object { $_.status -eq 'ACTIVE' })) {
  Write-Host "Creating cluster $Cluster"
  aws ecs create-cluster --cluster-name $Cluster --region $Region | Out-Null
}

$vars = @{
  ACCOUNT_ID=$AccountId; REGION=$Region; IMAGE_URI=$image; CLUSTER=$Cluster
  EXECUTION_ROLE_ARN=$ExecutionRoleArn; TASK_ROLE_ARN=$TaskRoleArn
  EFS_ID=$EfsId; LOG_GROUP=$LogGroup; LLM_PROVIDER=$LlmProvider
  LLM_MODEL=$LlmModel; MANIFEST_CONTAINER_PATH=$ManifestPath
}

function Register-TaskDef([string]$template) {
  $obj = Render-Template "$root\deploy\ecs\$template" $vars
  if (-not $EfsId) { $obj.volumes = @(); $obj.containerDefinitions[0].mountPoints = @() }
  $obj.cpu = "$Cpu"; $obj.memory = "$Memory"
  $tmp = [IO.Path]::GetTempFileName() + '.json'
  ($obj | ConvertTo-Json -Depth 20) | Set-Content $tmp
  Write-Host "Registering task definition from $template"
  $arn = (aws ecs register-task-definition --cli-input-json "file://$tmp" `
        --region $Region --query 'taskDefinition.taskDefinitionArn' --output text)
  Remove-Item $tmp
  Write-Host "  -> $arn"
  return $arn
}

$subnetCsv  = $Subnets -join ','
$sgCsv      = $SecurityGroups -join ','
$netCfg     = "awsvpcConfiguration={subnets=[$subnetCsv],securityGroups=[$sgCsv],assignPublicIp=$AssignPublicIp}"

# ---------- service mode --------------------------------------------------------
if ($Mode -in 'service','both') {
  $tdArn = Register-TaskDef 'taskdef.service.json'
  $existing = (aws ecs describe-services --cluster $Cluster --services $Project `
               --region $Region --query 'services[?status==`ACTIVE`].serviceName' --output text)
  $lb = @(); if ($AlbTargetGroupArn) {
    $lb = @('--load-balancers', "targetGroupArn=$AlbTargetGroupArn,containerName=datavigil,containerPort=8000")
  }
  if ($existing) {
    Write-Host "Updating service $Project"
    aws ecs update-service --cluster $Cluster --service $Project `
      --task-definition $tdArn --desired-count $DesiredCount `
      --force-new-deployment --region $Region | Out-Null
  } else {
    Write-Host "Creating service $Project"
    aws ecs create-service --cluster $Cluster --service-name $Project `
      --task-definition $tdArn --desired-count $DesiredCount `
      --launch-type FARGATE --network-configuration $netCfg `
      --enable-execute-command --region $Region @lb | Out-Null
  }
  Write-Host "Dashboard: task ENI on port 8000 — open http://<task-ip>:8000" +
             $(if ($AlbTargetGroupArn) { ' (via your ALB)' } else { '' })
}

# ---------- batch mode ----------------------------------------------------------
if ($Mode -in 'batch','both') {
  $tdArn = Register-TaskDef 'taskdef.batch.json'
  Write-Host "Batch task registered. Run once now with:"
  Write-Host "  aws ecs run-task --cluster $Cluster --launch-type FARGATE --task-definition $tdArn --network-configuration `"$netCfg`""

  if ($ScheduleExpression) {
    if (-not $SchedulerRoleArn) {
      $SchedulerRoleArn = "arn:aws:iam::${AccountId}:role/datavigil-scheduler"
      Write-Warning "No -SchedulerRoleArn; assuming $SchedulerRoleArn exists (needs ecs:RunTask + iam:PassRole — see README)."
    }
    $schedVars = $vars + @{
      TASKDEF_ARN=$tdArn; SCHEDULER_ROLE_ARN=$SchedulerRoleArn
      SUBNET_1=$Subnets[0]; SUBNET_2=($Subnets[$Subnets.Count-1])
      SECURITY_GROUP=$SecurityGroups[0]
    }
    $sched = Render-Template "$root\deploy\eventbridge\schedule.example.json" $schedVars
    $sched.ScheduleExpression = $ScheduleExpression
    $tmp = [IO.Path]::GetTempFileName() + '.json'
    ($sched | ConvertTo-Json -Depth 20) | Set-Content $tmp
    Write-Host "Creating EventBridge schedule 'datavigil-nightly-batch' ($ScheduleExpression)"
    aws scheduler create-schedule --cli-input-json "file://$tmp" --region $Region | Out-Null
    Remove-Item $tmp
  }
}

Write-Host "`nDone." -ForegroundColor Green

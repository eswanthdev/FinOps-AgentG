# Hosting the synthetic demo on AWS

For the active cloud-connected UI, use [Cloud UI release/rollback](../../docs/CLOUD_UI.md) and the
[documentation index](../../docs/README.md). The commands below describe the original hosting bootstrap.
The current UI uses `Dockerfile.cloud` and `release_cloud_ui.py`; changing only this stack's ImageUri does not
replace the running container. Release/rollback IDs are retained in local `var/` records and on the server.

**Status: deployed from this runbook, with the investigator live on Nova Lite.** **Synthetic data only.** Do not load
real billing data on this setup.

Every step below creates or changes paid AWS resources. Each one needs explicit approval first. Do not put credentials
in code or chat: sign in on your own machine with `aws sso login`.

## What you need first

- A separate non-production AWS account, region `eu-central-1`.
- The AWS CLI v2 and Docker on the machine that deploys.
- A domain or subdomain whose DNS you control, for example `costlens.example.com`.

## 1. Certificate

Request an ACM certificate for the domain in `eu-central-1`, add the DNS validation record, and wait until it is issued.

## 2. Image repository

```bash
aws cloudformation deploy --region eu-central-1 --stack-name costlens-ecr --template-file deploy/aws/costlens-ecr.yaml
```

## 3. Build and push the image

Tags are immutable, so use a new tag for every build, such as the short commit id. Build for `linux/amd64`.
On Windows, run the login below in Git Bash: Windows PowerShell 5.1 pipes broke it with a 400 error.

```bash
aws ecr get-login-password --region eu-central-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.eu-central-1.amazonaws.com
docker build --platform linux/amd64 --build-arg WITH_BEDROCK=true -t <repository-uri>:<tag> .
docker push <repository-uri>:<tag>
```

## 4. Main stack

```bash
aws cloudformation deploy --region eu-central-1 --stack-name costlens-demo --template-file deploy/aws/costlens-demo.yaml --capabilities CAPABILITY_IAM --parameter-overrides DomainName=<domain> CertificateArn=<arn> ImageUri=<repository-uri>:<tag> RepositoryArn=<arn> CognitoDomainPrefix=<prefix> BudgetEmail=<email>
```

The investigator uses Amazon Nova Lite through the `eu.amazon.nova-lite-v1:0` inference profile, so requests stay on EU
regions. Model calls are paid per use. The Claude client exists but got a 403 here and is not used.

Point the domain at the `LoadBalancerDns` output with a CNAME record.

## 5. Add users

Sign-up is off. Add each person in the Cognito user pool. Cognito sends them a temporary password.

## 6. Check before sharing the link

- [ ] `http://<domain>` redirects to HTTPS.
- [ ] Opening the site asks for a login.
- [ ] Port 8765 on the server's public IP does not answer.
- [ ] The target group shows the server as healthy.
- [ ] The app shows "Synthetic data" and "Deterministic mode".
- [ ] Logs appear in the `/costlens/demo` log group.
- [ ] A daily snapshot appears after the first scheduled run.
- [ ] The budget alert email address is right.
- [ ] The top bar says "AI-assisted mode", and a question shows "What the agent did".
- [ ] With the model turned off, answers fall back to fixed rules and say so.

## Update

Push a new tag, then open a session with `aws ssm start-session --target <ServerId>` and replace the container with the
new image. Changing `ImageUri` on the stack alone does not do this, because the start script only runs on first boot.
Expect a short outage.

## Rollback

Run the previous image tag the same way. If the server is lost, restore a snapshot.

## Remove everything

```bash
aws cloudformation delete-stack --region eu-central-1 --stack-name costlens-demo
```

The ECR stack needs its images deleted first. Snapshots made by the lifecycle policy stay until they are deleted by hand.

## Known limits

- One server, so updates and failures cause downtime.
- All users share one workspace and see each other's changes.
- MFA is off in the template. Turn it on if your company requires it.
- The login is not linked to app records, so "Approved" and "Implemented" stay unverified.
- The server has a public IP for outbound traffic only. The security group is what blocks inbound access, and the app
  trusts that, so never add an inbound rule to it.
- Costs in the plan were estimates from memory. Check the AWS pricing calculator first.

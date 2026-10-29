# Incident response: a secret or an account ID in the repository

What to do when a secret (a Hugging Face token, an AWS access key or any other credential) or an AWS account ID is found
in this public repository, in a fork of it or in your own copy. To report it, use the channel in
[SECURITY.md](../SECURITY.md).

Treat a value as exposed from the moment it was pushed to a public repository: clones, forks and caches copy it within
minutes, and deleting the file later does not take those copies back. That is why the credential comes first and the
clean-up second.

## 1. Stop using the credential: rotate or revoke it first

- **Hugging Face token.** Revoke it in the access tokens settings of the account that owns it
  (`https://huggingface.co/settings/tokens`), then create a new read token. If you keep it in Secrets Manager
  (`hf_token_secret_arn`), store the new value there.
- **AWS access key.** Deactivate it, then delete it:

  ```bash
  aws iam update-access-key --user-name <user> --access-key-id <key-id> --status Inactive
  aws iam delete-access-key --user-name <user> --access-key-id <key-id>
  ```

  Create a new key only if you still need a long-term one; temporary credentials (IAM Identity Center or an assumed
  role) are better. If temporary credentials of a role leaked, revoke the active sessions of that role in the IAM
  console.
- **AWS account ID.** An account ID is not a secret on its own, so there is nothing to rotate, but with role and bucket
  names it helps to target the account. Remove it (step 2) and check that no resource policy trusts it without other
  conditions.

Rotating is what makes a leaked credential useless. The steps below only limit who can still see it.

## 2. Remove it from the default branch

Open a pull request that deletes the value, or replaces an account ID with the placeholder `123456789012`, and merge
it. In your fork, commit the fix to its default branch. New clones no longer get the value, but it is still in the git
history, in the other branches and tags, in the pull request refs and in every fork.

## 3. Check what the credential was used for

- For an AWS key, `aws iam get-access-key-last-used --access-key-id <key-id>` shows its last use, and the CloudTrail
  event history (90 days of management events, per region) shows what it did. Run this in each region you use:

  ```bash
  aws cloudtrail lookup-events --lookup-attributes AttributeKey=AccessKeyId,AttributeValue=<key-id>
  ```

  Look for resources you did not create (GPU instances, IAM users, roles and keys, Capacity Blocks) and for unexpected
  charges in the Billing console. If you find activity that is not yours, contact AWS Support.
- For a Hugging Face token, check the repositories and settings of the account it belongs to for changes you did not
  make.

## 4. Purge the history

- Rewriting the history with `git filter-repo --sensitive-data-removal` (git-filter-repo 2.47 or newer) and a force push
  of every branch and tag removes the value from your repository, but `git filter-repo` is not enough on its own: GitHub
  still serves the old commits from the pull request refs (`refs/pull/*`, read-only) and from cached views, and every
  fork keeps its own copy.
- Contact GitHub Support (`https://support.github.com`) to remove the cached views and the pull request refs that still
  point at the old commits. GitHub decides case by case, and only after the credential is rotated.
- Ask the owners of the forks to delete their fork or to clean it: you cannot rewrite a fork you do not own.
- Tell everyone who cloned the repository to clone it again: a rewritten history does not merge with the old one.

GitHub documents the procedure in
[Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

## 5. Report it

If the value is in this repository (not only in your fork), report it through the channel in
[SECURITY.md](../SECURITY.md), the AWS vulnerability reporting page. Do not open a public GitHub issue: it would point
at the value again.

## 6. Re-scan before you push again

- [ ] `uv run --with-requirements tests/requirements-test.txt pytest tests/publish -q` passes. These are the
  sanitization guards of the repository: no account ID other than the placeholder `123456789012`, no e-mail address,
  local path or internal host, no Hugging Face token in the docs.
- [ ] `uv run --with-requirements tests/requirements-test.txt pytest tests/scripts/test_152_stage_model_hygiene.py -q`
  passes: no tracked file holds a string shaped like a Hugging Face token.
- [ ] `detect-secrets scan` reports no finding you have not reviewed.
- [ ] `git log --all --oneline -S '<value>'` prints nothing (run it in a shell whose history you clear afterwards).
- [ ] `git ls-files | grep -E '\.(tfstate|tfvars)$'` prints nothing: no Terraform state and no tfvars is tracked.
- [ ] The new credential works where the sample needs it, and the old one is refused.

## Where secrets live in this sample

- `HF_TOKEN` comes only from the process environment: export it in the shell that runs `scripts/10-stage-model.sh`.
  It is never written to `config/sample.env`, `manifests/config/platform.env` or `terraform/cluster/terraform.tfvars`:
  `scripts/gen_env.py` refuses it and overwrites both env files, and the tfvars has no token variable. The script hands
  it to the cluster on stdin as the short-lived Secret `node-ops/hf-token` and deletes that Secret when it exits.
- `hf_token_secret_arn` is the optional Secrets Manager ARN of the token. Terraform grants `secretsmanager:GetSecretValue`
  on it to the model-prefetch role and never creates or reads the secret: rotate the value in Secrets Manager. The ARN
  is not a secret, but it carries your account ID.
- AWS credentials come from your shell (an AWS CLI profile or the environment), never from a file of this repository.
- The account ID is in no tracked file: the docs and examples use the placeholder `123456789012`, and Terraform reads
  the real one from `data.aws_caller_identity`. The files that hold the real values, the Terraform state, the tfvars,
  `manifests/config/platform.env` and `config/sample.env`, are gitignored; keep them out of commits.

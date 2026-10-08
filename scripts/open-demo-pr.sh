#!/usr/bin/env bash
# Open the first demo PR. It bumps the demo resource in nonprod AND production so one PR shows
# every behaviour: `cmn / plan` is skipped (untouched), nonprod and production post HCP-style
# plan comments, and after a squash merge the promotion runs cmn -> nonprod -> production,
# pausing at `production / apply` for your approval.
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="poc/first-plan-$(date +%s)"
git switch -c "$BRANCH"
for env in nonprod production; do
  sed -i.bak 's/length = 2/length = 3/' "environments/$env/main.tf" && rm -f "environments/$env/main.tf.bak"
done
git commit -am "poc: bump random_pet length in nonprod and production" >/dev/null
git push -u origin "$BRANCH"
gh pr create --title "POC: first Terraform plan" \
  --body "Touches environments/nonprod and environments/production. Expect: cmn / plan skipped, plan comments for nonprod and production, approval gate on production after merge."
gh pr view --web

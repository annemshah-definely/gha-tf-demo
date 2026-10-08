#!/usr/bin/env bash
# Configure the GitHub side of the Terraform pipeline in one go, using the gh CLI:
#
#   * environments cmn / nonprod / production
#   * production: you as required reviewer (self-review allowed, so a solo POC works),
#                 deployments only from `main`
#   * repo merge settings: squash + rebase only, delete branch on merge
#   * branch ruleset from .github/rulesets/main.json (required plan checks, up-to-date branches)
#   * repository variable TERRAFORM_VERSION
#
# Usage: scripts/bootstrap-github.sh [owner/repo]     (defaults to the repo of the current directory)
# Needs: gh (https://cli.github.com) logged in with repo admin rights. Re-running is safe.
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
RULESET_FILE="$(dirname "$0")/../.github/rulesets/main.json"
TF_VERSION="${TERRAFORM_VERSION:-1.15.x}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✔ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }

gh auth status >/dev/null 2>&1 || { echo "gh is not logged in. Run: gh auth login"; exit 1; }
[ -f "$RULESET_FILE" ] || { echo "Ruleset file not found at $RULESET_FILE - run this from the repo root."; exit 1; }

say "Repository: $REPO"
IS_PRIVATE=$(gh repo view "$REPO" --json isPrivate --jq .isPrivate)
PLAN=$(gh api user --jq '.plan.name // "unknown"')
ok "visibility: $([ "$IS_PRIVATE" = true ] && echo private || echo public) · account plan: $PLAN"
if [ "$IS_PRIVATE" = true ] && [ "$PLAN" = "free" ]; then
  warn "Private repo on GitHub Free: required reviewers, deployment branch policies and rulesets"
  warn "are only available on PUBLIC repos for this plan. The pipeline will still plan/apply, but"
  warn "the production gate and the merge rules will not be enforced. For the POC, make it public:"
  warn "    gh repo edit $REPO --visibility public --accept-visibility-change-consequences"
fi

say "Environments"
USER_ID=$(gh api user --jq .id)
for env in cmn nonprod; do
  if gh api "repos/$REPO/environments/$env" >/dev/null 2>&1; then
    ok "$env already exists - left untouched"
  else
    gh api -X PUT "repos/$REPO/environments/$env" \
      --input - <<<'{"wait_timer":0,"reviewers":[],"deployment_branch_policy":null}' >/dev/null
    ok "$env created (no protection rules)"
  fi
done
# production: keep any reviewers you already configured, make sure you are one of them,
# allow self-review (solo POC) and restrict deployments to main.
REVIEWERS=$(gh api "repos/$REPO/environments/production" \
  --jq "([.protection_rules[]? | select(.type==\"required_reviewers\") | .reviewers[]? | {type: .type, id: .reviewer.id}] + [{type:\"User\", id: $USER_ID}]) | unique_by(.id)" \
  2>/dev/null || echo "[{\"type\":\"User\",\"id\":$USER_ID}]")
gh api -X PUT "repos/$REPO/environments/production" --input - >/dev/null <<EOF
{"wait_timer":0,"prevent_self_review":false,
 "reviewers":$REVIEWERS,
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
EOF
ok "production: required reviewers = existing + you (self-review allowed for the POC)"
if gh api -X POST "repos/$REPO/environments/production/deployment-branch-policies" \
     --input - <<<'{"name":"main","type":"branch"}' >/dev/null 2>&1; then
  ok "production deploys only from main"
else
  ok "production branch policy already present (or not available on this plan)"
fi

say "Merge settings"
gh repo edit "$REPO" --enable-merge-commit=false --enable-squash-merge=true \
  --enable-rebase-merge=true --delete-branch-on-merge=true >/dev/null
ok "merge commits off · squash + rebase on · delete branch on merge"

say "Branch ruleset"
NAME=$(sed -n 's/^[[:space:]]*"name":[[:space:]]*"\(.*\)",$/\1/p' "$RULESET_FILE" | head -1)
EXISTING=$(gh api "repos/$REPO/rulesets" --jq ".[] | select(.name==\"$NAME\") | .id" 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  gh api -X PUT "repos/$REPO/rulesets/$EXISTING" --input "$RULESET_FILE" >/dev/null && ok "updated ruleset '$NAME'"
elif gh api -X POST "repos/$REPO/rulesets" --input "$RULESET_FILE" >/dev/null 2>&1; then
  ok "created ruleset '$NAME' (required checks: cmn / plan, nonprod / plan, production / plan)"
else
  warn "could not create the ruleset (private repo on Free? import it manually once the repo is public)"
fi

say "Variables"
gh variable set TERRAFORM_VERSION --body "$TF_VERSION" --repo "$REPO"
ok "TERRAFORM_VERSION=$TF_VERSION"
ok "AWS_*_ROLE_ARN_* left unset - the demo environments run without cloud credentials"

say "Done. Next: scripts/open-demo-pr.sh"

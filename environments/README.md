# environments/

One directory per environment (= one HCP-style "workspace"). Each directory is a Terraform
root module with its own backend / state key. Shared code lives in `../modules/`.

Adding an environment:

1. `mkdir environments/<name>` with its own `main.tf` (backend, providers, module calls).
2. Add it to the `plan` matrix and an `apply-<name>` job in `.github/workflows/terraform.yml`.
3. Create a GitHub Environment named `<name>` (this is where approvals/gates live).
4. Add `AWS_PLAN_ROLE_ARN_<NAME>` / `AWS_APPLY_ROLE_ARN_<NAME>` repository variables.
5. Add `<name> / plan` to the required status checks in `.github/rulesets/main.json` and
   re-import the ruleset (or edit it in Settings -> Rules).

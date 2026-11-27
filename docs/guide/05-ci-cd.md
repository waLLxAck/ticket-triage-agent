# Phase 5: CI/CD with GitHub Actions and OIDC

## The pipeline (`.github/workflows/ci.yml`)

```
pull request ─► test ─────────────► eval (needs AWS + Langfuse) ─► comment on the PR
                 ruff, format,        53 tickets against the real
                 40 unit tests        model, gate on thresholds.toml
                 (free, ~1 min)       (~8 min, ~1p)
                                            │ fails ─► merge blocked (branch protection)

push to main ─► test ─► deploy (environment: production)
                         build zip ─► terraform apply ─► smoke test (routine + fraud ticket)
```

- **test** runs on everything. It needs no cloud access.
- **eval** runs on PRs. `concurrency: eval` queues runs, because they share one Bedrock quota.
  The report is posted as a PR comment, so a reviewer sees which metric moved and which
  tickets failed without opening logs.
- **deploy** runs only on `main`, inside the `production` GitHub environment, and ends with a
  smoke test against the live URL. The smoke test fails the deploy if a fraud ticket is
  auto-resolved.
- **Branch protection** on `main` requires `test` and `eval` to pass, so the quality gate is
  enforced rather than just advisory.

## OIDC: deploying with no stored keys

The old way was to create an IAM user, put its access key in GitHub secrets, and hope it never
leaks. Those keys last forever and work from anywhere.

The OIDC way:

```
GitHub Actions job                      AWS STS                         IAM role
──────────────────                      ───────                         ────────
1. "I need a token"  ──► GitHub's OIDC provider signs a JWT:
                         iss: token.actions.githubusercontent.com
                         aud: sts.amazonaws.com
                         sub: repo:waLLxAck/ticket-triage-agent:environment:production
2. AssumeRoleWithWebIdentity(JWT) ─────►  checks the signature against
                                          the OIDC provider registered
                                          in IAM (infra/bootstrap)
                                          checks the trust policy:  ─────► sub must equal
                                                                           ...:environment:production
3. ◄──── temporary credentials (1 hour) ──┘
```

- **Nothing to leak.** The credentials expire within the hour, and only a job from this repo,
  in this context, can get them.
- **Two roles, two scopes.**
  - `triage-gha-eval` trusts PRs and branches, and may only `bedrock:InvokeModel` on one model.
    A malicious PR can run up about a penny of model calls, and nothing more.
  - `triage-gha-deploy` trusts only `environment:production`, which GitHub only allows from
    `main`. It may manage only resources named `triage-*`, and only pass the app's own roles.
- **Forks:** a PR from a fork *does* produce a `...:pull_request` subject for this repo, so the
  trust policy alone isn't what stops it. What stops it is GitHub. For `pull_request` events
  from forks, the workflow gets no repository secrets and can't be granted `id-token: write`,
  so it can't request an OIDC token at all. Never switch this workflow to `pull_request_target`,
  which runs with the base repo's permissions.
- **Immutable subjects.** The first CI run failed with "Not authorized to perform
  sts:AssumeRoleWithWebIdentity". CloudTrail showed why: GitHub now signs
  `repo:waLLxAck@15944458/ticket-triage-agent@1474551602:pull_request`, which includes the
  owner and repo **IDs**, not just the names. The trust policy expected the old name-only
  format. With IDs, if the repo were deleted and someone recreated one with the same name, it
  wouldn't inherit the trust. `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`
  shows the prefix.
- **Permissions boundary.** Code review found that the deploy role could create a
  `triage-app-*` role with an `Action: "*"` policy, pass it to a Lambda, and so become account
  admin: a classic IAM privilege escalation. The deploy role can now only create or edit app
  roles that carry the `triage-app-boundary` policy. A boundary is a ceiling: effective
  permissions are the role's policy *intersected with* the boundary. The deploy role is also
  denied removing the boundary.

## Secrets and variables

| Name | Kind | Used by |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | secret | eval (tracing), deploy (written to SSM) |
| `ALERT_EMAIL` | secret | deploy (SNS and budget alerts) |
| `EVAL_ROLE_ARN`, `DEPLOY_ROLE_ARN` | variable | which role to assume (an ARN isn't a secret) |
| `TF_STATE_BUCKET` | variable | Terraform backend and Lambda zips |

They were set with `gh secret set`, piped from `.env`, so the values never appeared in a
terminal or in chat.

# Rook-CodeVolt fork stewardship

This document governs only the public
[`Rook-CodeVolt/hermes-agent`](https://github.com/Rook-CodeVolt/hermes-agent)
fork. It does not change, interpret, or make commitments for Nous Research or
the upstream
[`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)
project. Upstream documentation remains authoritative for upstream support,
security reporting, releases, and contribution policy.

## Current lifecycle and authority

The fork is **active, non-release stewardship infrastructure**. It holds
bounded, independently reviewed deltas and exact-commit verification evidence.
It does not publish or promise a separate package, installer, container image,
hosted service, support programme, compatibility guarantee, or release cadence.
A branch, tag, draft pull request, passing workflow, or fork commit is not a
release.

The `Rook-CodeVolt` fork owner governs fork lifecycle, accepted divergence,
sync decisions, and any future release decision. Nous Research and the upstream
maintainers govern upstream Hermes Agent support, releases, security policy,
and contributions. Work in this fork does not represent upstream acceptance or
authority.

Never place credentials, personal, customer, or production data, private
architecture or host details, recovery material, private vulnerability details,
or proprietary assets in this public repository, its issues, pull requests,
workflow logs, or artifacts.

## Support and security reporting

Choose the reporting route by where the affected behaviour originates:

- **Fork-only vulnerability:** submit a private report through the
  [`Rook-CodeVolt/hermes-agent` vulnerability-reporting form](https://github.com/Rook-CodeVolt/hermes-agent/security/advisories/new).
  Sign in to GitHub and choose **Report a vulnerability** on the fork's
  Advisories page; the link above opens that reporting flow. GitHub delivers
  the submission privately to this fork's administrators.
  Private vulnerability reporting for this fork was enabled and verified on
  2026-08-30. Do not open a public issue or pull request containing vulnerability
  details.
- **Inherited or upstream vulnerability:** follow the private upstream process
  in [SECURITY.md](SECURITY.md), which is intentionally preserved from
  `NousResearch/hermes-agent`.
- **Unclear boundary:** do not publish details. Use the fork's private form,
  identify the uncertainty, and let the fork administrators coordinate the
  correct private route.
- **Upstream product bug, feature request, or support question:** use the
  upstream routes linked from the inherited [README](README.md). This fork has
  no independent product-support commitment.
- **Non-sensitive fork-only proposal:** open a pull request against this fork
  with a minimal reproduction and exact base and head commits.

## Current divergence and upstream sync record

This inventory was reviewed on **2026-08-30 at 08:56 UTC**:

| Item | Exact reviewed state |
| --- | --- |
| Fork default branch | `main` at `bb626db42b076ef2709e38c2a729ab1d4367cf0a` |
| Fork main tree | `2c7f2d667ecb508dfb07474ef867ee21b88ea47d` |
| Last inherited upstream sync point (current merge base) | `3f36c87e1ebdfbf7d14a88229dc9be222c12ea89` |
| Upstream `main` head reviewed for sync | `2a598aad1c398e95b3325a0f100f5c28efa63d12` |
| Divergence at review | 1 fork-only commit; 129 upstream-only commits |

The sole fork-only commit at that baseline is
[`bb626db42b076ef2709e38c2a729ab1d4367cf0a`](https://github.com/Rook-CodeVolt/hermes-agent/commit/bb626db42b076ef2709e38c2a729ab1d4367cf0a),
which adds
[`.github/workflows/fork-exact-head-ci.yml`](.github/workflows/fork-exact-head-ci.yml).
The workflow is fork-only exact-head verification infrastructure; it is not an
upstream workflow, release signal, or product-support commitment.

This is a point-in-time record, not a claim that the fork is synchronized.
Before any sync, fetch both repositories and re-read the exact heads, merge
base, unique commits, open work, workflows, dependencies, generated files,
migrations, and public-data implications. Fetching is read-only; merging,
rebasing, conflict resolution, or force-pushing requires separate review and
authority.

## Change and review gate

Every fork pull request must:

- identify exact base and head commits, scope, affected users or systems,
  acceptance criteria, and rollback;
- classify documentation impact and update affected documentation in the same
  pull request;
- record commands actually run, exact-head checks, skipped checks, and open
  limitations without presenting local evidence as provider or release state;
- preserve [LICENSE](LICENSE), the Nous Research attribution, and applicable
  third-party notices;
- avoid new dependencies, Actions, code, assets, services, releases, or provider
  changes unless they have separate explicit authority and review; and
- remain draft and unmerged until an independent reviewer validates the exact
  head. Passing automation is evidence, not acceptance or release authority.

Missing or contradicted ownership, security-route, divergence, license,
rollback, or public-data evidence blocks acceptance.

## Rollback and retirement

Rollback for a documentation-only change is a normal revert of the exact
accepted commit followed by link, diff-scope, and public-data verification.
Never rewrite shared history as a rollback mechanism. Code, workflow,
dependency, schema, data, or release changes need a change-specific checkpoint,
rollback trigger, commands, and post-rollback verification before acceptance.

Retirement, archival, deletion, a visibility change, or a new release/support
commitment requires a separate fork-owner decision. Preserve the upstream MIT
license and attribution in all copies or substantial portions of the software.

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

This inventory was reviewed on **2026-09-03 at 10:06 UTC**:

| Item | Exact reviewed state |
| --- | --- |
| Fork default branch | `main` at `8178548437d63a2357fce98fa0b76d560d720713` |
| Fork main tree | `841fba346379142226146eda47f199d05e563ef5` |
| Last inherited upstream sync point (current merge base) | `3f36c87e1ebdfbf7d14a88229dc9be222c12ea89` |
| Upstream `main` head reviewed for sync | `561b053f794a1781868bb032029d589c67708119` |
| Divergence at review | 3 fork-only commits; 1,414 upstream-only commits |

The fork-only history at that baseline comprises the exact-head verification
workflow in
[`bb626db42b076ef2709e38c2a729ab1d4367cf0a`](https://github.com/Rook-CodeVolt/hermes-agent/commit/bb626db42b076ef2709e38c2a729ab1d4367cf0a),
the stewardship documentation in
[`41ba5fbcc6ef6d4e8aefb529e2675e763ee6454c`](https://github.com/Rook-CodeVolt/hermes-agent/commit/41ba5fbcc6ef6d4e8aefb529e2675e763ee6454c),
and merge commit
[`8178548437d63a2357fce98fa0b76d560d720713`](https://github.com/Rook-CodeVolt/hermes-agent/commit/8178548437d63a2357fce98fa0b76d560d720713).
These are fork-only stewardship changes, not upstream workflows, release
signals, or product-support commitments.

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

Independent review does not require an external collaborator. A fresh model
review may satisfy the review gate when it receives the complete exact diff in
an isolated, non-persistent, read-only execution with no repository credentials
or write, merge, or deployment capability. Its structured verdict must bind the
exact head, retain findings and limitations, and be preserved by digest. The
fork owner or constrained broker verifies freshness and applies any review
label; the model cannot approve its own mutation or authorise merge. Higher-risk
security, production, release, or irreversible changes require a separate human
or dual-model escalation.

Missing or contradicted ownership, security-route, divergence, license,
rollback, or public-data evidence blocks acceptance.

## Fork CI runner policy

Required pull-request checks use standard GitHub-hosted runner labels available
to this public personal fork. Upstream larger-runner labels such as
`ubuntu-latest-96-core`, `ubuntu-latest-32-core`, and
`windows-latest-32-core` are not provisioned here and must not be used by the
required Python, JavaScript, Rust, Windows, or Nix lanes. A static regression
test enforces this fork boundary. Standard runners may take longer, but a slower
bounded check is preferable to an unavailable runner that remains queued. The
first live fork run measures the resulting durations; the Python lane has a
90-minute bound, the Nix cache-miss lane a 120-minute bound, and the JavaScript,
Rust, and Windows lanes 60-minute bounds. Restore bounded sharding rather than a
larger-runner dependency if a limit proves insufficient.

## Rollback and retirement

Rollback for a documentation-only change is a normal revert of the exact
accepted commit followed by link, diff-scope, and public-data verification.
Never rewrite shared history as a rollback mechanism. Code, workflow,
dependency, schema, data, or release changes need a change-specific checkpoint,
rollback trigger, commands, and post-rollback verification before acceptance.

Retirement, archival, deletion, a visibility change, or a new release/support
commitment requires a separate fork-owner decision. Preserve the upstream MIT
license and attribution in all copies or substantial portions of the software.

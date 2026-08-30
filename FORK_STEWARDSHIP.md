# Rook-CodeVolt fork stewardship

This document governs only the public `Rook-CodeVolt/hermes-agent` fork. It does
not change, interpret, or make commitments for Nous Research or the upstream
`NousResearch/hermes-agent` project. Upstream project documentation remains the
authority for upstream support, security reporting, releases, and contribution
policy.

## Current lifecycle and boundary

The fork is **active, non-release stewardship infrastructure**. It exists to
hold bounded, reviewed deltas, reproduce fixes against exact commits, and
prepare evidence or contributions that may later be proposed upstream.

The fork does not currently publish or promise a separate package, installer,
container image, hosted service, support programme, compatibility guarantee, or
release cadence. A branch, tag, draft pull request, passing workflow, or fork
commit is not a release. Creating a release, deployment, package, image, public
service, or independent support commitment requires a separate decision by
Rook with security and operational review; it is outside ordinary pull-request
approval.

Never place credentials, customer or production data, private CodeVolt
architecture, hostnames, recovery material, private vulnerability details, or
proprietary assets in this public repository, pull requests, issues, workflow
logs, or release artifacts.

## Accountability, support, and escalation

| Responsibility | Accountable role | Exact boundary |
| --- | --- | --- |
| Fork lifecycle, accepted divergence, sync priority, release decision, retirement, and final programme acceptance | **Rook, fork steward** | Decides whether work belongs in this fork and whether a reviewed delta is accepted. |
| Inventory, documentation, test execution, rollback preparation, and draft-PR implementation | **Oliver, bounded executor** | May prepare reversible changes and evidence. Does not merge, release, deploy, change provider settings, or represent Nous Research. |
| Public-data, security, dependency, Action, provenance, and supply-chain review | **Maya, independent reviewer** | Reviews independently and blocks ambiguous rights, unsafe disclosure, mutable supply-chain references, or expanded authority. |
| Upstream project ownership, releases, and support | **Nous Research/upstream maintainers** | Not controlled by this fork. Use upstream routes and policies for upstream behaviour. |

For a fork-only defect or proposed fork delta, open a fork pull request with a
minimal reproduction and exact base/head commits. Route upstream product bugs,
feature requests, and support questions through the routes named by upstream.
For vulnerabilities, follow the private process in [SECURITY.md](SECURITY.md);
do not publish a security report in a fork issue or pull request. If ownership,
rights, sensitivity, or the correct reporting boundary is unclear, stop and
escalate privately to Rook before uploading evidence.

## Divergence and upstream sync policy

1. Record the exact fork base and upstream commit before work. Fetching is
   read-only; rebases, merges, force-pushes, and conflict resolutions are
   changes requiring their own review.
2. Prefer the smallest isolated branch and keep fork-only policy separate from
   upstream application behaviour. Do not silently rewrite application code to
   preserve a fork delta.
3. Before sync, inventory commits unique to each side, open work, workflows,
   dependencies, generated files, migrations, and public-data implications.
4. Re-test every retained delta on the proposed sync commit. Conflicts,
   semantic drift, uncertain licences, changed secret permissions, or unclear
   ownership are stop conditions, not invitations to guess.
5. A fork delta may be dropped only when Rook accepts the reason and rollback
   evidence. An upstream contribution is a separate proposal; upstream pull
   requests are external evidence and must not be edited, closed, merged, or
   otherwise represented as governed by this fork.
6. Keep the MIT notice in [LICENSE](LICENSE) with copies or substantial
   portions of the software. Preserve applicable third-party notices and
   attribution when syncing or redistributing material.

Rollback for a documentation-only fork change is a normal revert of the exact
accepted commit. For code, workflow, dependency, schema, data, or release
changes, the pull request must define a more specific checkpoint, rollback
trigger, commands, data implications, and post-rollback verification before
acceptance. Never rely on force-pushing shared history as the rollback plan.

## Change and documentation gate

Every fork pull request must:

- identify exact base/head commits, affected users/systems, owner, scope, and
  acceptance criteria;
- classify documentation impact and update the same pull request, or explain
  specifically why behaviour and handover remain accurate without a docs
  change;
- cover architecture, API/schema, configuration, security, operations,
  migration, rollback, release, support, and retirement effects where they
  apply;
- record commands actually run and distinguish local evidence, exact-head CI,
  skipped checks, and unresolved exceptions;
- keep new dependencies and Actions traceable to a canonical source, reviewed
  licence, exact version, and immutable digest or full commit SHA where the
  ecosystem supports one; explain any unavoidable mutable reference and name
  its update owner;
- record creator/source, canonical URL, version or commit, licence,
  redistribution/modification rights, attribution, generation method,
  accessibility information, and replacement path for new or changed assets;
- preserve [LICENSE](LICENSE) and all applicable notices; do not add material
  whose provenance, rights, or sensitivity is uncertain;
- provide an independent reviewer with enough evidence to compare material
  documentation claims against implementation; and
- remain a draft and unmerged until Rook accepts it after required independent
  review. Passing automation is evidence, not acceptance or release authority.

Documentation review is required whenever a change affects user-visible
behaviour, setup, configuration, dependencies, Actions, assets, trust
boundaries, permissions, operations, migration, rollback, support, release,
lifecycle status, or repository ownership. It is also required before a sync,
release decision, external handover, archival, or visibility change.

## Handover and periodic review

At each material sync or release decision, and at least when this policy itself
changes, the fork steward must be able to recover from repository evidence:

- current lifecycle state and accepted fork-only deltas;
- exact upstream relationship and last reviewed sync point;
- owners, review/escalation paths, tests, CI limitations, and open risks;
- dependency, Action, asset, licence, attribution, and provenance decisions;
- deployment/release state (currently none) and applicable rollback procedure.

Rook owns the accept/reject record. Oliver supplies bounded implementation and
verification evidence. Maya independently verifies the public/security and
supply-chain boundary. Missing or contradicted handover evidence blocks
acceptance; it must not be converted into an undocumented exception.

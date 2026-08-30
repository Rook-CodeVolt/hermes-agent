## What does this PR do?

<!-- Describe the change clearly. What problem does it solve? Why is this approach the right one? -->



## Related Issue

<!-- Link the issue this PR addresses. If no issue exists, consider creating one first. -->

Fixes #

## Type of Change

<!-- Check the one that applies. -->

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 🔒 Security fix
- [ ] 📝 Documentation update
- [ ] ✅ Tests (adding or improving test coverage)
- [ ] ♻️ Refactor (no behavior change)
- [ ] 🎯 New skill (bundled or hub)

## Changes Made

<!-- List the specific changes. Include file paths for code changes. -->

- 

## Fork stewardship and documentation impact

<!--
This fork's boundary is defined in ../FORK_STEWARDSHIP.md. State exact base and
head commits, the accountable owner, affected users/systems, and whether this is
a fork-only delta or an upstream candidate. Do not imply Nous Research approval.
-->

- Base commit:
- Head commit:
- Accountable owner:
- Fork-only delta or upstream candidate:
- Documentation impact (name updated files, or give a specific no-doc reason):
- Operations/migration/release effect:
- Rollback trigger, steps, and verification:

## Provenance, rights, and supply chain

<!--
For every new or changed asset, dependency, generated file, or Action, record
canonical source, exact version/commit, licence and required attribution,
redistribution/modification rights, immutable digest/full SHA where supported,
and update ownership. Write "None" only after checking the diff.
-->

- Assets/generated material:
- Dependencies:
- GitHub Actions or other executable references:
- Licence/NOTICE/attribution changes:

## How to Test

<!-- Steps to verify this change works. For bugs: reproduction steps + proof that the fix works. -->

1. 
2. 
3. 

## Checklist

<!-- Complete these before requesting review. -->

### Code

- [ ] I've read the [Contributing Guide](../CONTRIBUTING.md) and, for work in `Rook-CodeVolt/hermes-agent`, the [fork stewardship boundary](../FORK_STEWARDSHIP.md)
- [ ] My commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`fix(scope):`, `feat(scope):`, etc.)
- [ ] I searched for [existing PRs](https://github.com/NousResearch/hermes-agent/pulls) to make sure this isn't a duplicate
- [ ] My PR contains **only** changes related to this fix/feature (no unrelated commits)
- [ ] I've run `pytest tests/ -q` and all tests pass
- [ ] I've added tests for my changes (required for bug fixes, strongly encouraged for features)
- [ ] I've tested on my platform: <!-- e.g. Ubuntu 24.04, macOS 15.2, Windows 11 -->

### Documentation & Housekeeping

<!-- Check all that apply. It's OK to check "N/A" if a category doesn't apply to your change. -->

- [ ] I've classified documentation impact above and updated relevant documentation in this PR, or supplied a specific no-doc reason
- [ ] I've updated `cli-config.yaml.example` if I added/changed config keys — or N/A
- [ ] I've updated `CONTRIBUTING.md` or `AGENTS.md` if I changed architecture or workflows — or N/A
- [ ] I've considered cross-platform impact (Windows, macOS) per the [compatibility guide](../CONTRIBUTING.md#cross-platform-compatibility) — or N/A
- [ ] I've updated tool descriptions/schemas if I changed tool behavior — or N/A
- [ ] I've documented operations, migration, release, rollback, and support effects above — or explained why each is N/A
- [ ] New/changed assets and generated material have verified provenance, rights, licence/attribution, reproduction/use guidance, accessibility metadata, and update ownership — or N/A
- [ ] New/changed dependencies and Actions have canonical sources, reviewed licences, immutable versions/full SHAs where supported, and update ownership — or N/A
- [ ] I verified this public diff contains no credentials, customer/production data, private infrastructure or recovery material, private vulnerabilities, or unintended proprietary material
- [ ] A reviewer other than the author will validate material documentation and provenance claims against the implementation before acceptance

## For New Skills

<!-- Only fill this out if you're adding a skill. Delete this section otherwise. -->

- [ ] This skill is **broadly useful** to most users (if bundled) — see [Contributing Guide](../CONTRIBUTING.md#should-the-skill-be-bundled)
- [ ] SKILL.md follows the [standard format](../CONTRIBUTING.md#skillmd-format) (frontmatter, trigger conditions, steps, pitfalls)
- [ ] No external dependencies that aren't already available (prefer stdlib, curl, existing Hermes tools)
- [ ] I've tested the skill end-to-end: `hermes --toolsets skills -q "Use the X skill to do Y"`

## Screenshots / Logs

<!-- If applicable, add screenshots or log output showing the fix/feature in action. -->


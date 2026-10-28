## The release note learns to speak 📝

## The short version

- **Narrative release notes**: every release can now open with a hand-written title and a short
  version of what it brings, sitting above the generated changelog — the way Orca publishes.
- The pipeline bug that dropped v0.17.0's narrative is fixed: the summary is snapshotted at
  dispatch and never rides on the tagged bump commit that clears it.

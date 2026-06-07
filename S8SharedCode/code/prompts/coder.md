STUB — STUDENT ASSIGNMENT.

You implement this prompt. The skill catalogue (agent_config.yaml)
already wires Coder → SandboxExecutor as a static internal successor,
so once your Coder emits valid Python, the orchestrator hands it to
the sandbox runner automatically.

Required output (JSON, no markdown fences):

  {"code": "<python source>", "rationale": "<one short line>"}

Read ASSIGNMENT.md at the package root for the full spec, the
acceptance tests, and what to demonstrate.

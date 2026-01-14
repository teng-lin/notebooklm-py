# Agent Flashcards

## Card 1

**Q:** What is the core philosophy of the 'learn-claude-code' project?

**A:** Modern AI agents work because the model is trained to be an agent; our job is to give it tools and stay out of the way.

---

## Card 2

**Q:** What is the fundamental loop that every coding agent, like Claude Code, is based on?

**A:** A loop where the model calls tools until it's done, and the results of those tools are appended to the message history for the next iteration.

---

## Card 3

**Q:** According to the 'learn-claude-code' philosophy, the model is _____% of the agent, and the code is _____% of the agent.

**A:** 80, 20

---

## Card 4

**Q:** What is the core insight of the v0 `v0_bash_agent.py`?

**A:** Bash is all you need; a single `bash` tool is sufficient to provide full agent capability, including reading, writing, and executing.

---

## Card 5

**Q:** In `v0_bash_agent.py`, how is the concept of a subagent implemented without a dedicated 'Task' tool?

**A:** By recursively calling itself via a bash command (e.g., `python v0_bash_agent.py "subtask"`), which spawns an isolated process with a fresh context.

---

## Card 6

**Q:** What core insight does `v1_basic_agent.py` demonstrate?

**A:** The concept of 'Model as Agent,' where the model is the primary decision-maker, and the code just provides tools and runs the execution loop.

---

## Card 7

**Q:** What are the four essential tools introduced in `v1_basic_agent.py` that cover 90% of coding use cases?

**A:** `bash`, `read_file`, `write_file`, and `edit_file`.

---

## Card 8

**Q:** In an agent system, what is the purpose of the `read_file` tool?

**A:** To read the contents of an existing file, allowing the agent to understand code.

---

## Card 9

**Q:** Which tool in `v1_basic_agent.py` is used for surgical changes to existing code by replacing exact text?

**A:** The `edit_file` tool.

---

## Card 10

**Q:** What problem in multi-step tasks does `v2_todo_agent.py` aim to solve?

**A:** Context Fade, where the model loses focus or forgets steps in a complex plan because the plan is not explicitly tracked.

---

## Card 11

**Q:** What new tool is introduced in `v2_todo_agent.py` to enable structured planning?

**A:** The `TodoWrite` tool, which allows the agent to maintain and update a visible task list.

---

## Card 12

**Q:** What is the key design insight behind the constraints (e.g., max 20 items, one `in_progress` task) in the `TodoManager`?

**A:** Structure constrains AND enables; the constraints force focus and make complex task completion possible by providing scaffolding.

---

## Card 13

**Q:** In the `TodoManager`, what is the purpose of the `activeForm` field for a task item?

**A:** It provides a present-tense description of the action being performed for the task currently marked as `in_progress`.

---

## Card 14

**Q:** What problem arises when a single agent performs large exploration tasks before acting, as addressed in v3?

**A:** Context Pollution, where the agent's history fills with exploration details, leaving little room for the primary task.

---

## Card 15

**Q:** How does the v3 subagent mechanism solve the problem of context pollution?

**A:** It spawns child agents with isolated contexts for subtasks, so the main agent only receives a clean summary as a result.

---

## Card 16

**Q:** What is the name of the tool introduced in v3 to spawn child agents?

**A:** The `Task` tool.

---

## Card 17

**Q:** In the v3 subagent design, what is the purpose of the `AGENT_TYPES` registry?

**A:** To define different types of agents (e.g., 'explore', 'code', 'plan') with specific capabilities, prompts, and tool access.

---

## Card 18

**Q:** What is the key to context isolation when a subagent is executed in v3?

**A:** The subagent is started with a fresh, empty message history (`sub_messages = []`), so it does not inherit the parent's context.

---

## Card 19

**Q:** In the v3 `AGENT_TYPES` registry, what is the key difference in tool permissions for an 'explore' agent versus a 'code' agent?

**A:** The 'explore' agent has read-only tools (like `bash` and `read_file`), while the 'code' agent has access to all tools, including those that write files.

---

## Card 20

**Q:** What is the core insight of the v4 skills mechanism?

**A:** Skills are knowledge packages, not tools; they teach the agent HOW to do something, rather than just giving it a new capability.

---

## Card 21

**Q:** In v4, a Tool is what the model _____, while a Skill is how the model _____ to do something.

**A:** CAN do, KNOWS

---

## Card 22

**Q:** What is the paradigm shift that skills embody, moving away from traditional AI development?

**A:** Knowledge Externalization, where knowledge is stored in editable documents (`SKILL.md`) instead of being locked inside model parameters.

---

## Card 23

**Q:** What is the main advantage of Knowledge Externalization over traditional model fine-tuning?

**A:** Anyone can teach the model a new skill by editing a text file, without needing ML expertise, training data, or GPU clusters.

---

## Card 24

**Q:** What is the standard file format for defining a skill in the v4 agent?

**A:** A `SKILL.md` file containing YAML frontmatter for metadata and a Markdown body for instructions.

---

## Card 25

**Q:** What are the two required metadata fields in a `SKILL.md` file's YAML frontmatter?

**A:** `name` and `description`.

---

## Card 26

**Q:** What is the concept of 'Progressive Disclosure' in the v4 skills mechanism?

**A:** Loading knowledge in layers: first, lightweight metadata is always available, and second, the detailed skill body is loaded only when triggered.

---

## Card 27

**Q:** What is the name of the tool introduced in v4 that allows the model to load domain expertise on-demand?

**A:** The `Skill` tool.

---

## Card 28

**Q:** What is the critical implementation detail for how skill content is injected into the conversation to preserve the prompt cache?

**A:** The skill content is returned as a `tool_result` (part of a user message), not injected into the system prompt.

---

## Card 29

**Q:** Why is it a bad practice to inject dynamic information into the system prompt on each turn of an agent loop?

**A:** It invalidates the KV Cache, as the entire message prefix changes, leading to re-computation and drastically increased costs (20-50x).

---

## Card 30

**Q:** What is the KV Cache in the context of LLMs?

**A:** A mechanism that stores the computed key-value states of previous tokens in a sequence so they don't need to be re-calculated for subsequent tokens.

---

## Card 31

**Q:** A cache hit for an LLM API call requires that the new request has an _____ with the previous request.

**A:** exact prefix match

---

## Card 32

**Q:** Which of these is a cache-breaking anti-pattern in agent development: append-only messages or message compression?

**A:** Message compression, as it modifies past history and invalidates the cache from the point of replacement.

---

## Card 33

**Q:** To optimize for cost and performance with LLM APIs, you should treat the conversation history as an _____, not an editable document.

**A:** append-only log

---

## Card 34

**Q:** In the `learn-claude-code` project, how does using Skills represent a shift from 'training AI' to 'educating AI'?

**A:** It turns implicit knowledge that required training into explicit, human-readable documents that can be written, version-controlled, and shared.

---

## Card 35

**Q:** In v3, the pattern `Main Agent -> Subagent A -> Subagent B` is described as a strategy of _____.

**A:** Divide and conquer

---

## Card 36

**Q:** What is the purpose of the `safe_path` function in the provided agent code?

**A:** It's a security measure to ensure the file path provided by the model stays within the project's working directory.

---

## Card 37

**Q:** In the v1 agent's `run_bash` function, what is one reason a command might be blocked?

**A:** It is considered dangerous, containing patterns like `rm -rf /` or `sudo`.

---

## Card 38

**Q:** What does the v3 `get_tools_for_agent` function do?

**A:** It filters the list of available tools based on the specified `agent_type` to enforce capability restrictions.

---

## Card 39

**Q:** Why do subagents in the v3 demo not get access to the `Task` tool?

**A:** To prevent the possibility of infinite recursion (a subagent spawning another subagent).

---

## Card 40

**Q:** In v4, what information does the `SkillLoader` class initially load from all `SKILL.md` files at startup?

**A:** Only the metadata (name and description) from the YAML frontmatter, to keep the initial context lean.

---

## Card 41

**Q:** How does the v4 agent provide the model with hints about a skill's available resources, such as scripts or reference documents?

**A:** When a skill's content is loaded, it includes a list of files found in optional subdirectories like `scripts/` and `references/`.

---

## Card 42

**Q:** What is the primary difference between the `write_file` and `edit_file` tools?

**A:** `write_file` creates or completely overwrites a file, while `edit_file` performs a surgical replacement of specific text within an existing file.

---

## Card 43

**Q:** The core agent loop `while True: response = model(messages, tools) ...` demonstrates that the _____ controls the loop.

**A:** model

---

## Card 44

**Q:** In v3, what is the role of a 'plan' agent type?

**A:** To analyze a codebase and produce a numbered implementation plan without modifying any files.

---

## Card 45

**Q:** The v4 `SkillLoader`'s `parse_skill_md` function uses a regular expression to separate the _____ from the Markdown body.

**A:** YAML frontmatter

---

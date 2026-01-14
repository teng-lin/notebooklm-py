# Agent Quiz

## Question 1
What is the core philosophy of the `learn-claude-code` project, as stated in its documentation?

- [ ] The agent's performance is primarily determined by clever engineering and complex code loops.
- [x] The model itself is the agent, and the surrounding code's main job is to provide it with tools and manage the execution loop.
- [ ] The number and variety of tools available to the agent are the most critical factors for its success.
- [ ] Effective AI agents must rely solely on their internal, pre-trained knowledge without using external tools.

**Hint:** Consider the stated ratio of importance between the model and the code in the project's philosophy.

## Question 2
In the `v0_bash_agent.py` implementation, how is the concept of a subagent achieved?

- [ ] By using a dedicated `Task` tool that spawns a child agent from a registry.
- [x] By recursively executing the script as a new, isolated process via a `bash` command.
- [ ] By creating a new thread within the parent process to handle the sub-task.
- [ ] By instantiating a new agent class within the code that maintains a separate history list.

**Hint:** Think about how this minimalist agent uses its single tool to delegate work.

## Question 3
What primary problem is the `TodoWrite` tool in `v2_todo_agent.py` designed to solve for the agent?

- [ ] The agent polluting its context window with excessive file content during exploration.
- [ ] The agent's inability to write new files or modify existing ones.
- [x] The model losing focus or forgetting the overall plan during complex, multi-step tasks.
- [ ] The agent not knowing which type of specialized subagent to use for a task.

**Hint:** This tool helps make the agent's internal thought process visible and persistent.

## Question 4
According to the v3 documentation, what is the main advantage of using subagents?

- [ ] It allows the agent to learn new domain-specific expertise from `SKILL.md` files.
- [ ] It provides a mechanism for structured planning and tracking task completion.
- [ ] It enables the agent to run multiple `bash` commands in parallel for faster execution.
- [x] It isolates the context of a sub-task, preventing the main agent's history from becoming polluted.

**Hint:** Consider the problem that arises when an agent reads many large files to prepare for a small change.

## Question 5
The v4 documentation draws a clear distinction between 'Tools' and 'Skills'. What is this distinction?

- [x] Tools define what an agent *can do* (its capabilities), while Skills define *how* an agent knows to perform a task (its expertise).
- [ ] Tools are built-in functionalities, while Skills are third-party plugins that must be downloaded.
- [ ] Tools are used for general-purpose tasks like reading files, while Skills are only for code generation.
- [ ] Tools are implemented in Python code, whereas Skills are implemented using shell scripts.

**Hint:** Think about the difference between having a hammer and knowing how to build a house.

## Question 6
What is the key benefit of the "Knowledge Externalization" paradigm introduced in v4?

- [x] It allows the agent's knowledge to be version-controlled, audited, and edited in plain text by anyone, without requiring model training.
- [ ] It forces the agent to write all its internal thoughts to an external log file for easier debugging.
- [ ] It caches the model's large parameter weights in the local file system to speed up agent initialization.
- [ ] It requires the agent to use external web APIs for all information, preventing it from using outdated internal knowledge.

**Hint:** This paradigm shift makes customizing an agent's expertise as easy as editing a text document.

## Question 7
To maintain cost-efficiency by preserving the KV Cache, how does the `v4_skills_agent.py` inject skill content into the conversation?

- [ ] By dynamically updating the system prompt with the new skill information before each model call.
- [x] By appending the skill content as a `tool_result` message, which doesn't alter the preceding message history.
- [ ] By performing a lightweight fine-tuning operation on the model in real-time.
- [ ] By storing the skill content in a separate memory buffer that is not part of the model's main context.

**Hint:** The correct method avoids changing the beginning or middle of the conversation history.

## Question 8
Which of these is NOT one of the four essential tools introduced in the `v1_basic_agent.py` to cover most coding use cases?

- [ ] `bash`
- [ ] `read_file`
- [x] `TodoWrite`
- [ ] `edit_file`

**Hint:** The first version of the agent focused on core capabilities for interacting with the file system and shell, not on complex planning.

## Question 9
What is the fundamental logic of the "core agent loop" described throughout the project?

- [ ] The agent first creates a complete, unchangeable plan and then executes each step in sequence.
- [x] The model makes a tool call, the tool's result is added to the history, and this cycle repeats until the model stops calling tools.
- [ ] The user provides a precise sequence of tools for the agent to execute.
- [ ] The agent's code analyzes the prompt and selects a single, optimal tool to resolve the entire request.

**Hint:** It is a repetitive cycle of thinking, acting, and observing the results.

## Question 10
In the `v3` subagent mechanism, what is the purpose of the `AGENT_TYPES` registry?

- [x] To define different subagent archetypes with specific roles, prompts, and restricted toolsets.
- [ ] To list all the available skills and their descriptions for the `v4` agent.
- [ ] To keep a real-time log of all active subagent processes and their current status.
- [ ] To provide the input schema and validation rules for the `TodoWrite` tool.

**Hint:** This configuration allows the main agent to choose the right kind of 'specialist' for a sub-task.

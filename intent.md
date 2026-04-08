## Project Vision: The Lean Agent Harness (The "Toolbox" Framework)

The goal is to create a modular, "Unix-style" orchestration layer that manages AI agents as specialized tools. It replaces bloated AI platforms with a thin Python script and a repository of Markdown files, ensuring the agent stays focused, high-quality, and grounded.

### 1. Core Intent & Philosophy
* **Markdown-Driven Intelligence:** The "brains" live in a dedicated **Harness Repo**. Each file defines a specific role (e.g., Senior Developer, QA) or skill. This keeps instructions clean, version-controlled, and easy to edit in VS Code.
* **The "Baton Pass" Workflow:** Instead of one long, confusing session, the system works in loops. One agent completes a sub-task, writes the "next steps" into a file, and "passes the baton" to the next specialized agent.
* **Heartbeat Automation:** A lightweight Python script (the "manager") watches for task updates. When a task is ready, it selects the right tool and initiates the work session.
* **Total Transparency:** You see every "thought" and terminal command in real-time. There are no hidden processes—just the raw power of the CLI directed by your custom instructions.

### 2. Spatial Grounding (The Two-Repo System)
This framework explicitly separates the **"Brain"** from the **"Hands"**:
* **The Toolbox (Harness Repo):** Where the agent’s personality, skills, and long-term session history live. This is the persistent "Home Base."
* **The Worksite (Work Repo):** The specific project folder where the agent is currently working (e.g., your Selsbakkhøgda apartment project).
* **The Bridge:** The Python harness acts as a guide, waking the agent up with a clear "Spatial Awareness" command: *"You are working at the Worksite, but your instructions and memory are back at the Toolbox. Do not wander outside these two zones."*

### 4. Success Criteria
* **No Context Bloat:** The agent only knows what it needs for the current task.
* **Portability:** The harness can be "plugged in" to any project folder instantly.
* **Indefinite Progress:** Through "Baton Passing," the workflow can run autonomously across multiple sessions until the overarching goal is met.
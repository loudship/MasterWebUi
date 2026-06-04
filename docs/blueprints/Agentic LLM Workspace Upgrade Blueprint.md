# **Phase 2 Upgrade Blueprint: Scaffolding a Zero-Trust Agentic Local LLM Laboratory**

## **1\. Architectural Imperatives and the Zero-Trust Agentic Transition**

The deployment of a baseline local Large Language Model (LLM) workspace establishes a static, responsive paradigm where isolated containerized microservices facilitate standard user-prompted text generation and basic Retrieval-Augmented Generation (RAG). However, upgrading this pristine environment into an autonomous, agentic laboratory introduces profound architectural complexities. An agentic system fundamentally transitions the LLM from a passive responder to an active computational orchestrator capable of reading dynamic web protocols, interpreting complex visual document structures, writing arbitrary software logic, and executing that logic within the host environment.1  
Executing this upgrade without compromising the strictly isolated, zero-trust posture established in the Phase 1 build requires scaffolding a series of highly specialized, defensive subsystems. Autonomous execution demands that all external inputs—such as scraped web code and parsed document layouts—be mathematically sanitized, and all model-generated outputs—specifically arbitrary Python execution scripts—be constrained by host-level kernel boundaries.2 Standard deployment patterns fail to account for the unique integration vectors required to bridge Open WebUI's frontend orchestrator with the independent Python Pipelines ecosystem and external headless browsers.1  
This comprehensive execution blueprint is designed explicitly for an autonomous coding agent to consume and implement. It defines the exact installation vectors, dependency graphs, Python package requirements, and declarative manifest modifications required to integrate the industry's most powerful community tools into the workspace. The orchestration of these tools involves distinct deployment vectors: some must operate as isolated Docker containers on the bridge network, others must be cloned as Python scripts into the persistent ./data/pipelines volume, and several require native integration into the Open WebUI internal SQLite database. By adhering strictly to this Directed Acyclic Graph (DAG) deployment strategy, the execution agent will safely construct a multi-modal, agentic laboratory capable of independent research, data extraction, and programmatic execution without exposing the host operating system to severe vulnerabilities.

## **2\. The Heavy-Duty Scraper Subsystem: Overcoming Egress Limitations**

The Phase 1 architecture relies on SearXNG for web search orchestration. While SearXNG excels at privacy-respecting metasearch and aggregating multi-engine results, it operates fundamentally as a metadata aggregator. It returns search snippets and URLs, but fails critically when Open WebUI attempts to scrape the underlying full-text content of those URLs using its native fetching module. Modern target domains employ aggressive bot-protection heuristics, Cloudflare challenges, and heavy JavaScript-rendered Document Object Models (DOM). When Open WebUI's native mechanisms encounter these barriers, the egress request times out, triggers HTTP 403 Forbidden errors, or retrieves useless CAPTCHA payloads, subsequently poisoning the RAG context window with noise and causing the LLM to hallucinate.1  
To achieve elite agentic data gathering, the architecture must integrate a dedicated, headless-browser-backed scraping engine capable of executing JavaScript, bypassing anti-bot measures, and transforming raw DOMs into LLM-optimized Markdown formatting.5

### **2.1 Evaluating the Extraction Ecosystem**

The community ecosystem offers several powerful extraction engines, most notably Firecrawl, Jina AI Reader, and Crawl4AI. Each presents distinct architectural trade-offs regarding zero-trust compliance and integration complexity.  
Firecrawl is recognized for its comprehensive capabilities, converting websites into clean, LLM-ready markdown, and efficiently handling complex site navigations.6 An autonomous agent can integrate Firecrawl into the Open WebUI Pipelines ecosystem by cloning the Open-WebUI-Pipelines repository into the ./data/pipelines volume.7 However, utilizing Firecrawl inherently violates the zero-trust, air-gapped constraint of the local laboratory unless the operator is willing to self-host the massive Firecrawl infrastructure, which is computationally prohibitive for a standard workstation. Default usage requires routing local RAG data through external API endpoints, exposing the system to third-party telemetry.7  
Similarly, the Jina AI Reader API offers exceptional input size reduction by employing fixed content filtering to reduce token payloads before they hit the LLM.8 A community tool, web\_scrape\_jina.py, can be deployed natively within Open WebUI to leverage this service, injecting markdown directly into the chat.8 Like Firecrawl, this approach routes traffic through external servers (https://r.jina.ai/), creating an unacceptable egress vector for a defensively postured laboratory.9  
The optimal, strict zero-trust solution is Crawl4AI, deployed natively alongside the stack as a completely isolated, headless container operating entirely within the local Docker bridge network.4 Crawl4AI functions as an asynchronous web crawler that renders JavaScript and integrates directly into AI pipelines by outputting structured JSON and Markdown without relying on external SaaS endpoints.5

### **2.2 The Crawl4AI and Proxy Integration Strategy**

Because Open WebUI's internal parser expects a specific REST API format for external web loaders, an intermediary translation layer is required to bridge the orchestrator with the Crawl4AI instance. The execution agent must deploy crawl4ai-proxy, a lightweight bridging service that normalizes the Open WebUI egress request and translates it into a Crawl4AI-compatible payload.4  
The execution agent must implement the following architectural modifications to the central docker-compose.yml manifest to scaffold this subsystem. These containers must be attached to the existing custom user-defined bridge network (e.g., llm-net) to ensure internal DNS resolution.

| Service Definition | Image Target | Architectural Purpose | Network & Resource Constraints |
| :---- | :---- | :---- | :---- |
| crawl4ai-proxy | ghcr.io/lennyerik/crawl4ai-proxy:latest | Acts as the translation bridge between Open WebUI and the headless scraper. | Must map LISTEN\_PORT=8000 and CRAWL4AI\_ENDPOINT=http://crawl4ai:11235/crawl.4 |
| crawl4ai | unclecode/crawl4ai:0.6.0-r2 | The core headless Chromium engine executing the asynchronous scraping logic. | Must allocate shm\_size: 1g to prevent Chromium from crashing due to shared memory exhaustion during DOM rendering.4 |

### **2.3 Orchestrator Configuration and Routing Overrides**

Once the containers are instantiated, the Open WebUI orchestrator must be reconfigured to abandon its native fetcher in favor of the proxy bridge. The execution agent must inject these specific environment variables into the Open WebUI container definition or the centralized .env file. Due to the PersistentConfig paradox identified in Phase 1, the agent must ensure these variables are seeded flawlessly upon the first container boot, as Open WebUI will commit them to its persistent SQLite database.1

| Environment Variable | Required String Value | Functional Execution Purpose |
| :---- | :---- | :---- |
| WEB\_LOADER\_ENGINE | external | Instructs the Open WebUI internal routing controller to utilize an external protocol rather than its native fetching modules.4 |
| EXTERNAL\_WEB\_LOADER\_URL | http://crawl4ai-proxy:8000/crawl | Establishes the exact internal Docker bridge network route to the proxy.4 |
| EXTERNAL\_WEB\_LOADER\_API\_KEY | local\_bypass | A mandatory field required by the internal parser to satisfy syntax checks; the value is arbitrary but cannot be null.4 |

By implementing this subsystem, the agentic LLM can independently request full-page scrapes of dynamic, heavily protected web targets. When the LLM generates a search intent, SearXNG returns the URLs, and Open WebUI routes the fetching request to crawl4ai-proxy. The proxy commands Crawl4AI to execute a headless Chromium instance, bypass anti-bot heuristics, render the JavaScript payload, strip out navigation boilerplate, and return pure, semantically dense Markdown. This fundamentally eliminates the RAG context buffer overflow vulnerabilities while preserving absolute network isolation.1

## **3\. Advanced Vision and Layout Extraction: The Docling Subsystem**

In a standard LLM workspace, document ingestion relies on elementary text splitters, such as PyPDF or Apache Tika, which blindly strip text from documents using basic character delimitation.12 This naive extraction process destroys table structures, ignores hierarchical document headings, and completely discards embedded images. When an autonomous agent attempts to reason over complex scientific literature, financial reports, or visual schematics, this structural data loss results in catastrophic analytical failures, as the spatial relationship between data points is entirely obliterated.13  
To equip the laboratory with elite document understanding, the autonomous agent must replace the legacy extractors with Docling, a sophisticated vision-language parsing engine that maintains precise semantic structures, Markdown table formats, and bounding-box coordinates for embedded objects.15

### **3.1 Scaffolding the Docling Microservice**

Docling requires significant computational overhead, including advanced layout models and optical character recognition (OCR) engines like Tesseract, and must operate as a dedicated microservice alongside the core orchestrator.15 The autonomous agent must append the Docling service definition to the docker-compose.yml manifest, explicitly managing its asynchronous worker configuration to prevent state loss during heavy document processing.

| Configuration Directive | Variable Assignment | Systemic Rationale for Agent Execution |
| :---- | :---- | :---- |
| Container Image | quay.io/docling-project/docling-serve:latest | Pulls the official, specialized microservice image containing the advanced layout analysis binaries.15 |
| Port Mapping | 5001:5001 | Establishes the internal TCP socket for Open WebUI to transmit document payloads.15 |
| UVICORN\_WORKERS | 1 | A critical stabilization parameter. Setting this strictly to 1 prevents internal task routing state loss and race conditions when processing massive PDF files.15 |
| DOCLING\_SERVE\_MAX\_SYNC\_WAIT | 300 | Increases the timeout threshold to 300 seconds, ensuring the connection does not drop while the OCR engine parses hundreds of pages.15 |

### **3.2 State Overrides and Multimodal Image Extraction**

Following the deployment of the docling-serve container, Open WebUI must be forcefully configured to route all document ingestion requests to this new engine. The agent must inject the following configuration parameters into the Open WebUI environment to override the default Tika or PyPDF extractors:

1. CONTENT\_EXTRACTION\_ENGINE=docling: Instructs the backend routing controller to bypass native chunking.  
2. DOCLING\_ENGINE\_URL=http://docling-serve:5001: Provides the internal bridge DNS address.  
3. ENABLE\_OPENAI\_IMAGE\_URL=True: A highly specific capability flag. When Docling parses a document, it identifies embedded images and extracts their layout metadata. If this flag is enabled, Open WebUI appends each image's Base64 encoding alongside its spatial metadata directly into the document stream. If the operator utilizes a multimodal local model (like LLaVA or a vision-capable Qwen iteration via LM Studio), the LLM gains access to both the raw image data and its exact position on the page, enabling precise queries such as analyzing charts located on specific pages.13

By offloading document extraction to Docling, the agentic laboratory ensures that tabular data is retrieved as perfect Markdown tables and hierarchical relationships are preserved, exponentially increasing the accuracy of subsequent RAG operations without relying on external cloud parsers.

## **4\. Unrestricted Multimedia Context via YouTube Transcript Extraction**

When autonomous agents interact with video media through standard RAG tools, the system typically scrapes subsets of subtitles, generates vector embeddings, and retrieves highly fragmented semantic chunks based on keyword similarity. This approach shatters the chronological and logical flow of the video, crippling the model's ability to summarize extensive lectures, synthesize step-by-step programming tutorials, or understand overarching narratives.8 Furthermore, YouTube frequently blocks IPs belonging to cloud providers or heavy scrapers, resulting in severe RequestBlocked or IpBlocked exceptions when standard pipelines attempt to fetch subtitles.18  
To engineer a superior agentic capability, the agent must install the YouTube Transcript Provider tool. This specifically crafted Python plugin fundamentally bypasses the RAG vectorization pipeline entirely. By leveraging the langchain-yt-dlp library, the tool extracts the full chronological transcript in English and injects it directly into the LLM's primary context window, allowing the model to process the entire video script holistically.8

### **4.1 Dependency Injection in the Container Namespace**

The YouTube Transcript Provider is not an isolated container; it operates as an Open WebUI native Tool. Native Tools are Python scripts that execute directly within the Open WebUI container's internal Python environment. Because this specific tool relies on external libraries not present in the default Open WebUI Docker image, the execution agent must perform a dynamic dependency injection before the tool can be initialized. Failure to inject these dependencies will result in a fatal ImportError when the LLM attempts to execute the tool, crashing the agentic reasoning loop.19  
The autonomous agent must execute a shell command to penetrate the Open WebUI container namespace and install the binaries via pip. This command must be structured as follows and executed on the host machine during the scaffolding sequence:  
docker exec \-u 0 \<open-webui-container-name\> pip install langchain-yt-dlp youtube\_transcript\_api 19  
Executing this command with \-u 0 ensures root privileges within the container, allowing the package manager to successfully write the libraries to the system site-packages directory.

### **4.2 Tool Configuration and Valve Settings**

Once the internal Python namespace contains the required binaries, the agent must deploy the Tool payload. The agent can inject the raw Python script (youtube-transcript-provider.py) directly into the Open WebUI interface by navigating to Workspace \-\> Tools, or via API injection into the backend database.2  
This specific tool defines UserValves, which are configuration parameters exposed to the LLM and the operator. The agent should configure the default initialization parameters to ensure maximum reliability:

* TRANSCRIPT\_LANGUAGE="en,en\_auto": Instructs the youtube\_transcript\_api to prioritize manually created English subtitles, but gracefully fall back to auto-generated English subtitles if manual ones are unavailable.19  
* GET\_VIDEO\_DETAILS=True: Commands the tool to utilize yt-dlp to fetch the video's title, description, and metadata alongside the transcript, enriching the context window for the LLM.19

By directly injecting the full transcript, the agentic LLM gains an unfragmented understanding of the media, allowing for comprehensive summarization and exact timestamp referencing, wholly bypassing the limitations of vector database chunking.

## **5\. Kernel-Level Sandboxing for Autonomous Code Execution**

The defining characteristic of an elite AI laboratory is the ability of the LLM to act as a Code Interpreter—writing, debugging, and independently executing its own Python or Bash code to solve deterministic mathematical problems, manipulate complex data structures, or interact with local network resources dynamically.21 However, allowing an LLM to execute arbitrary, un-sandboxed Python code directly within the Open WebUI or Pipelines container represents an existential security vulnerability. A hallucinated prompt, or a maliciously injected payload derived from a scraped web page, could easily trigger system-level command execution. This would allow the LLM to destroy the internal container filesystem, exfiltrate cryptographic API keys, or pivot across the internal Docker bridge network to compromise the host operating system or the LM Studio inference engine.1  
To achieve zero-trust agentic code execution, the system must deploy the Safe Code Execution toolkit engineered by EtiennePerot. This mechanism abandons standard Python execution environments in favor of gVisor (runsc), a highly secure, user-space kernel created by Google that intercepts and filters all system calls between the Python interpreter and the host operating system, guaranteeing absolute isolation.2

### **5.1 The Linux Kernel cgroups v2 Imperative and the "Hard Way"**

The implementation of gVisor requires precise, granular manipulation of the Linux kernel's Control Groups (cgroups v2) architecture. gVisor utilizes cgroups to enforce strict limitations on the maximum RAM, CPU scheduling, and maximum file generation of the sandboxed code. This ensures that the LLM cannot trigger a Denial of Service (DoS) attack on the host hardware via infinite loops, memory leaks, or recursive fork bombs.2  
By default, Docker containers are entirely stripped of the privileges required to manage nested cgroups. To enable gVisor without completely destroying the security posture of the Open WebUI container by utilizing the highly dangerous \--privileged=true flag, the execution agent must implement a precise capability escalation known as "The Hard Way".3  
The autonomous agent must append the following critical security directives to the open-webui service definition within the docker-compose.yml manifest:

| Docker Compose Directive | Value | Kernel Security Justification |
| :---- | :---- | :---- |
| security\_opt | seccomp:unconfined | gVisor requires a complex matrix of system calls (such as ptrace) to emulate the guest kernel. The default Docker seccomp profile blocks these calls. Removing it at the Docker layer is safe because gVisor aggressively re-applies a much stricter filter around the execution of the actual LLM-generated code.3 |
| security\_opt | apparmor:unconfined | Disables the default AppArmor profile which interferes with user-space kernel emulation.3 |
| security\_opt | label:type:container\_engine\_t | Sets the SELinux label required for nested containerization engines.3 |
| volumes (Bind Mount) | /sys/fs/cgroup:/sys/fs/cgroup:rw | Binding the host's cgroup file system into the container with read/write permissions allows the internal Python supervisor script to dynamically construct the hierarchical cgroup structure (e.g., $INITIAL/codeeval\_$NUM/sandbox/leaf). This ensures every LLM execution operates in an ephemeral resource boundary.2 |

Failure to correctly map the cgroups will result in a fatal Sandbox runtime failed or /sys/fs/cgroup/cgroup.subtree\_control: device or resource busy error when the LLM attempts to execute code, entirely halting the agentic workflow.25

### **5.2 Tool Implementation and Context Management**

With the host kernel permissions appropriately scoped, the agent must install the run\_code.py tool natively within Open WebUI.2 When deployed as a Toolkit, this grants the LLM the autonomous ability to decide when to generate code, execute it internally, and read the STDOUT back into its context window.27  
The Safe Code Execution Tool defines an intricate set of configuration parameters (Valves) that dictate the boundaries of the sandbox. The execution agent must inject these overrides into the Open WebUI environment variables (.env file) to ensure the system is seeded securely upon initialization.2

* CODE\_EVAL\_VALVE\_OVERRIDE\_NETWORKING\_ALLOWED=True: Allows the LLM to write scraping scripts or API callers inside the sandbox.  
* CODE\_EVAL\_VALVE\_OVERRIDE\_MAX\_RUNTIME\_SECONDS=30: Establishes a hard timeout to prevent infinite execution loops from locking the inference thread.2  
* CODE\_EVAL\_VALVE\_OVERRIDE\_REQUIRE\_RESOURCE\_LIMITING=True: A critical defensive flag. It forces the tool to crash gracefully if the cgroup kernel mapping fails, rather than executing the code insecurely without memory boundaries.2  
* CODE\_EVAL\_VALVE\_OVERRIDE\_AUTO\_INSTALL=True: Instructs the tool to automatically download the correct runsc (gVisor) binary from GitHub for the host's architecture (AMD64 or ARM64) upon the first tool invocation, bypassing the need for manual binary placement.2

Once installed, the human operator or the agent must edit the target LLM model's capabilities and explicitly enable the "Run Code" Tool. When the LLM encounters a mathematical anomaly, a complex logic puzzle, or a requirement for custom data parsing, it will autonomously generate a Python payload, pass it to the gVisor sandbox, and integrate the calculated results into its ongoing reasoning loop.2

## **6\. Managing Persistent Agentic State: Long-Term Memory Integration**

An elite laboratory requires the LLM to possess persistent continuity across isolated conversation threads. Without long-term memory, an autonomous agent cannot iteratively learn from its codebase generation failures, remember overarching user system preferences, or track complex, multi-day research objectives.29  
The Open WebUI community offers external vector-based pipelines, such as Mem0 (mem0-owui), which can be cloned directly into the ./data/pipelines volume. When deployed as a Pipeline filter, Mem0 intercepts the conversation stream, extracts relevant context, and stores it in an external vector database.29 However, relying on external pipeline filters introduces unnecessary database dependencies and synchronization lag, complicating the zero-trust architecture.  
To maintain optimal performance and isolation, the execution agent must configure Open WebUI's natively integrated Agentic Memory management mechanisms.32

### **6.1 The Autonomous Memory Tool Suite**

Open WebUI's core architecture utilizes five distinct, system-level function calls that grant the LLM explicit read, write, and delete permissions over a dedicated user-preference SQLite vector space.32 By utilizing native memory, the system avoids the need to scaffold additional Python dependencies within the Pipelines container.  
The five native tools the agent leverages are 32:

1. add\_memory: Proactive storage of newly deduced facts, research parameters, or constraints learned during the conversation.  
2. search\_memories: Semantic querying of the persistent database for contextual injection, utilizing the internal embedding model.  
3. replace\_memory\_content: The ability to update an outdated assumption (e.g., changing a target API endpoint or updating a coding language preference).  
4. delete\_memory: Removing deprecated logic or finalized task parameters to prevent context bloat.  
5. list\_memories: Retrieving the holistic timeline of user preferences.

### **6.2 Model Optimization and KV Cache Preservation**

For this native memory subsystem to function reliably alongside the Code Interpreter and Heavy Scraper, the local LLM hosted in LM Studio must be highly proficient in strict JSON schema generation and Tool Calling.32 Frontier models (like quantized deepseek-coder or advanced Llama-3 implementations) are mandatory for this level of autonomous orchestration.  
A significant architectural failure point during prolonged agentic execution is severe memory pressure and latency spikes caused by continuous context shifting. When memories or heavy RAG data (such as Crawl4AI markdown or Docling tables) are injected into the user message, the position of the data constantly shifts as the conversation lengthens. This forces the external LLM provider to completely recalculate the entire attention matrix on every single turn, causing massive KV Cache invalidation.1  
To mitigate this, the execution agent must ensure that the RAG and Memory context injections are mathematically isolated from the sliding conversation window. The agent must inject the RAG\_SYSTEM\_CONTEXT=True variable into the Open WebUI .env file. This critical directive forces the orchestrator to lock injected memories and scraped web content exclusively into the system prompt. By locking the data at the top of the context window, the local model can utilize KV prefix caching, allowing for near-instant follow-up responses even when reasoning over thousands of tokens of agentic context.1

## **7\. The Deterministic Execution Matrix: Scaffolding and Validation**

To prevent catastrophic configuration drift, POSIX permission failures, and asynchronous race conditions, the autonomous coding agent must execute the scaffolding of this Phase 2 blueprint in a strict, sequential protocol. Deviating from this Directed Acyclic Graph (DAG) matrix will trigger silent internal routing failures, specifically between the proxy bridges, the Pipelines execution environment, and the cgroup mapping layers.1

### **Step 1: Pre-flight Kernel and Dependency Checks**

The agent must assess the host environment to confirm compatibility with the advanced Vision and Sandboxing subsystems before initiating Docker API commands.

* Verify the host operating system kernel supports Linux cgroups v2, which is an absolute prerequisite for gVisor resource limiting.3  
* Verify the Docker daemon permissions allow for the mounting of /sys/fs/cgroup.  
* Assess host memory availability. The addition of docling-serve and crawl4ai (with its mandatory 1GB shm\_size) requires a minimum of 4GB of available RAM overhead strictly dedicated to the container subsystems, distinct from the host's LLM inference requirements.4

### **Step 2: Synthesis of the Unified Deployment Manifest**

The agent must mathematically merge the new microservices into the core infrastructure-as-code state file (docker-compose.yml), ensuring all nodes communicate across the secure bridge network.

* Inject the crawl4ai-proxy and crawl4ai service definitions, binding them strictly to the llm-net network to prevent external host exposure.4  
* Inject the docling-serve definition, explicitly enforcing the UVICORN\_WORKERS=1 constraint to preserve document processing state.15  
* Modify the pre-existing open-webui service definition. The agent must inject the seccomp:unconfined, apparmor:unconfined, and label:type:container\_engine\_t security options, alongside the /sys/fs/cgroup volume binding to unlock the gVisor sandbox potential.3

### **Step 3: Global Environment Variable Injection**

The agent must inject the routing logic that binds the Open WebUI orchestrator to the new peripheral engines. This is achieved by updating the centralized .env file.

* Append the Crawl4AI routing parameters: WEB\_LOADER\_ENGINE=external and EXTERNAL\_WEB\_LOADER\_URL=http://crawl4ai-proxy:8000/crawl.4  
* Append the Docling routing parameters: CONTENT\_EXTRACTION\_ENGINE=docling and DOCLING\_ENGINE\_URL=http://docling-serve:5001.15  
* Append the gVisor Tool constraints: CODE\_EVAL\_VALVE\_OVERRIDE\_MAX\_RUNTIME\_SECONDS=30, CODE\_EVAL\_VALVE\_OVERRIDE\_REQUIRE\_RESOURCE\_LIMITING=True, and CODE\_EVAL\_VALVE\_OVERRIDE\_AUTO\_INSTALL=True.2

### **Step 4: Staggered Initialization Sequence**

To prevent internal DNS resolution timeouts and SQLite database locking errors during the initial boot sequence, the agent must initialize the infrastructure sequentially.

* Execute the backend deployment: docker compose up \-d crawl4ai crawl4ai-proxy docling-serve.  
* Implement a programmatic wait state (e.g., 15 seconds) to allow headless Chromium to initialize its shared memory blocks and the Docling Uvicorn server to bind to port 5001\.15  
* Execute the frontend deployment: docker compose up \-d open-webui to restart the orchestrator, forcing it to ingest the new cgroup permissions and environment variables.

### **Step 5: Internal Namespace Dependency Injection**

The agent must penetrate the Open WebUI container namespace to install the critical Python libraries required by the native YouTube Transcript script. Because this is a Tool rather than a Pipeline filter, the dependencies must be installed in the orchestrator container, not the ./data/pipelines volume.

* Execute the strict terminal command targeting the orchestrator: docker exec \-u 0 \<open-webui-container-name\> pip install langchain-yt-dlp youtube\_transcript\_api.19

### **Step 6: Programmatic Validation and Assertion**

The agent must cryptographically and programmatically prove that the internal routing and kernel boundaries are functional before yielding control. Standard Docker health checks (HEALTHCHECK CMD) only prove the internal application loop is running, not that inter-container networking is operational. The agent must execute the following terminal assertions:

| Validation Target | Execution Command Structure | Expected Output / Assertion Criteria | Systemic Failure Implication |
| :---- | :---- | :---- | :---- |
| Crawl4AI Proxy Bridge | docker exec \<open-webui-container\> curl \-s \-o /dev/null \-w "%{http\_code}" "http://crawl4ai-proxy:8000" | HTTP Code 200 or 404 (indicating a valid API response frame). | The scraper will fail; RAG will drop web content or timeout on Cloudflare protection headers. |
| Docling Vision Parser | docker exec \<open-webui-container\> curl \-s \-o /dev/null \-w "%{http\_code}" "http://docling-serve:5001/health" | HTTP Code 200\. | Total failure of PDF uploads, layout analysis, and tabular data extraction. |
| Kernel Sandbox Escalation | docker exec \<open-webui-container\> cat /proc/self/status | grep CapEff | Presence of escalated kernel capabilities allowing cgroup filesystem writes. | gVisor will throw a fatal cgroup.subtree\_control not found error; the Code Interpreter will crash entirely, neutralizing agentic coding capabilities.26 |

### **Step 7: Finalizing the Elite Laboratory Handover**

Upon successful programmatic validation, the autonomous coding agent has finalized the infrastructure scaffolding. The environment now possesses an airtight, zero-trust boundary capable of interpreting heavily obfuscated web data, understanding complex multimodal document layouts, bypassing RAG truncation via direct media transcript injection, and safely executing arbitrary Python logic generated by the local LLM.  
The final operational step requires human-in-the-loop intervention. The autonomous agent must synthesize a log instructing the human operator to access the Open WebUI graphical interface, navigate to the Workspace \-\> Tools administrative panel, and paste the raw Python payloads for the run\_code.py (Sandbox) and youtube-transcript-provider.py scripts directly into the database.2 The operator must then navigate to the specific Local Model settings, enable Native Function Calling (Agentic Mode), and toggle the installed tools to active.2  
By executing this comprehensive Phase 2 blueprint, the baseline LLM workspace is permanently transformed into a highly resilient, multi-modal, autonomous agentic laboratory, fully prepared for uninterrupted, secure computational research.

#### 


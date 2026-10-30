# Sujeeth Foundation Wiki MCP Server

## Project Overview

The Sujeeth Foundation Wiki MCP Server is a Model Context Protocol (MCP)-based service that enables AI assistants and intelligent applications to discover, search, and retrieve information from the Sujeeth Foundation knowledge base.

Built on the existing Wiki Search API, the project aims to make the foundation's documentation accessible to AI-powered workflows through a standardized interface. By implementing MCP, the server will provide a consistent integration layer between the foundation's knowledge resources and compatible AI clients, including ChatGPT, Claude, and Microsoft Copilot Studio, subject to each platform's integration capabilities.

## Project Objectives

- **Centralized Knowledge Access:** Provide a unified interface for accessing information stored in the Sujeeth Foundation wiki.
- **Semantic and Keyword Search:** Enable AI assistants to find relevant documentation using keyword search and, in future iterations, semantic search powered by LangSearch.
- **AI Assistant Integration:** Support MCP-compatible AI clients through standardized tools and resource access.
- **Reliable Information Retrieval:** Retrieve relevant wiki pages, snippets, source references, and URLs to support accurate, context-aware responses.
- **Extensible Architecture:** Establish a foundation for integrating ontology models, structured knowledge relationships, and advanced retrieval capabilities.
- **Cloud Deployment:** Host the service on Microsoft Azure using a scalable, maintainable architecture.

## Core Capabilities

### 1. Wiki Search
Connect to the Sujeeth Foundation wiki and identify relevant pages based on user queries. The existing Wiki Search API provides keyword-based relevance scoring, snippets, source URLs, and configurable result limits.

### 2. MCP Tool Interface
Expose wiki search and related knowledge-retrieval operations as MCP tools, allowing compatible AI clients to request information through a standardized protocol.

### 3. Knowledge Retrieval
Return relevant content alongside page titles, source paths, and links, allowing AI assistants to ground their responses in the foundation's documentation.

### 4. Cache and Refresh
Reuse cached wiki content to reduce unnecessary retrieval operations and provide a refresh mechanism for loading updated documentation.

### 5. Future Semantic Search
Integrate LangSearch or another suitable retrieval service to improve results for conceptual queries, synonyms, and questions whose wording differs from the original wiki content.

### 6. Ontology-Driven Knowledge Discovery
Introduce an ontology model to represent concepts, entities, relationships, and domain-specific terminology associated with the foundation. This can help AI assistants retrieve information based on meaning and relationships rather than keyword matches alone.

## Proposed Architecture

The solution will use a modular architecture:

1. **Knowledge Source:** The Sujeeth Foundation GitHub wiki.
2. **Search Layer:** The existing Python-based Wiki Search API, deployed as an Azure Function App.
3. **MCP Server:** A separate Python service that exposes search and knowledge-retrieval functionality through MCP tools.
4. **Retrieval and Intelligence Layer:** LangSearch and ontology-based retrieval capabilities, introduced as future enhancements.
5. **AI Clients:** Compatible assistants and applications, including ChatGPT, Claude, and Microsoft Copilot Studio, depending on their respective MCP or connector support.
6. **Hosting and Operations:** Microsoft Azure for deployment, configuration, monitoring, and operational management.

## Technology Stack

- **Programming Language:** Python
- **API Hosting:** Azure Functions
- **Knowledge Source:** GitHub Wiki
- **Search API:** Python HTTP endpoints
- **Integration Protocol:** Model Context Protocol (MCP)
- **AI Integration Targets:** ChatGPT, Claude, and Microsoft Copilot Studio
- **Future Retrieval Enhancement:** LangSearch
- **Knowledge Modeling:** Ontology-based representation
- **Version Control and CI/CD:** GitHub and GitHub Actions

## Security and Reliability

The MCP server will be designed to protect access to the knowledge services and support reliable operation. Planned considerations include secure credential management, appropriate authentication and authorization, request validation, error handling, logging, and controlled access to connected tools.

Public wiki content can be retrieved without a GitHub token when anonymous access is sufficient. Credentials should be stored in Azure configuration or an appropriate secrets-management service rather than committed to source control.

## Implementation Roadmap

**Phase 1 — Wiki Search API**
- Deploy the existing Python Wiki Search API to Azure.
- Support keyword-based search, result snippets, caching, and content refresh.
- Validate search results against the public wiki.

**Phase 2 — MCP Server**
- Develop a standalone MCP server as a separate Azure-hosted service.
- Expose wiki search and page-retrieval operations as MCP tools.
- Add secure connectivity to the existing search API.

**Phase 3 — AI Client Integration**
- Configure supported AI clients to connect to the MCP server.
- Validate tool discovery, query execution, result handling, and source attribution.
- Document client-specific configuration and authentication requirements.

**Phase 4 — Advanced Retrieval**
- Evaluate LangSearch or an equivalent semantic retrieval service.
- Develop an ontology model for the foundation's knowledge domain.
- Combine keyword, semantic, and ontology-based retrieval where appropriate.

**Phase 5 — Production Readiness**
- Improve monitoring, error reporting, performance, and security.
- Add automated tests and deployment checks.
- Evaluate search quality and establish ongoing maintenance procedures.

## Expected Outcome

The project will establish a reusable AI integration layer for the Sujeeth Foundation knowledge base. It will allow compatible AI assistants to search and retrieve foundation documentation through a standardized MCP interface, while creating a path toward semantic search, ontology-driven discovery, and more sophisticated knowledge-based AI applications.

The long-term goal is to make the foundation's knowledge easier to discover, interpret, and use across multiple AI platforms without duplicating the underlying documentation or maintaining a separate knowledge integration for every client.

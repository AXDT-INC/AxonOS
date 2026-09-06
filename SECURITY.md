## Security Policy

If you believe you have found a security vulnerability in AxonOS, please **do not** open a public issue with exploit details.

### Reporting

- **Preferred**: Use GitHub’s private reporting flow (Security Advisories) for this repository.
- **If that is not available**: Send an email with the details to security@axondao.io

### Scope

This repository includes container build scripts, a browser-accessible desktop stack (noVNC/VNC and WebRTC streaming), the session gate (`axonos_gate/`: wallet authentication, on-chain deposit verification, the x402 settlement signer, session scheduling with Docker-socket access, SSH and web-terminal gateways, guest demo mode), and the AxonAI/OpenCode agent with its permission policy (`axonos_assistant/opencode.json`). Reports covering any of these components are in scope; payment verification, the x402 hot wallet, launcher/Docker isolation, and agent permission enforcement are of particular interest.

### Safe Harbor

We support good-faith security research. Please avoid privacy-invasive testing and do not disrupt production systems.


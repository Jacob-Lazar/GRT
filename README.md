# GRT: Gate Runtime

Lightweight Observability SDK for AI Agents.

## Installation

```bash
pip install grt
```

## Usage

```python
import gate

# Configure the SDK
gate.configure(
    service_name="my-agent",
    # ...
)

@gate.observe
def my_agent_step():
    pass
```

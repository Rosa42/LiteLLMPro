# Stream probe report template (B6)

| field | description |
|-------|-------------|
| layer | `layer1_upstream` or `layer2_proxy` |
| deployment_id | e.g. `opencode-a-chat-deepseek-v4-flash` |
| protocol | `openai_chat` or `anthropic_messages` |
| model | logical model name |
| pass | boolean |
| ttfe_ms | time to first event (ms) |
| duration_ms | total probe duration |
| event_count | SSE events observed |
| saw_done | OpenAI: `[DONE]` seen |
| saw_message_stop | Anthropic: `message_stop` seen |
| error_in_body | HTTP 200 body contained error event |
| notes | free text, no secrets |

Scripts: `scripts/probe_stream_upstream.py`, `scripts/probe_stream_proxy_e2e.py`

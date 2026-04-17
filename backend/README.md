# llm-council-workflow backend

FastAPI + LangGraph backend for the workflow runtime.

Google Gemini models can run in either of these modes:

- `GOOGLE_GEMINI_BACKEND=gemini_api` using `GEMINI_API_KEY` or `GOOGLE_API_KEY`
- `GOOGLE_GEMINI_BACKEND=vertex_ai` using ADC plus `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION`

When Vertex AI mode is enabled, authenticate locally with:

```bash
gcloud auth application-default login
```

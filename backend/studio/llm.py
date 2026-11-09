"""The AI endpoint: any OpenAI-compatible server (OpenRouter, LiteLLM, Ollama, vLLM, ...), set on the Settings page.
Everything that talks to a model goes through here, so the address, the key and the quirks of the server live in one place."""
import json
import time
from urllib.parse import urlparse
import httpx

from . import activity, config, settings


def base_url():
    return (settings.get('LLM_BASE_URL') or '').rstrip('/')


def is_openrouter():
    return urlparse(base_url()).hostname == 'openrouter.ai'


def available():
    """An address is set, and a key too unless the server is one that needs none (a local Ollama, an internal gateway)."""
    return bool(base_url()) and (bool(settings.get('OPENROUTER_API_KEY')) or not is_openrouter())


def headers():
    key = settings.get('OPENROUTER_API_KEY')
    return {**({'Authorization': f'Bearer {key}'} if key else {}), 'X-Title': 'Curltale AI'}


def extras(reasoning=None):
    """Request fields only OpenRouter understands; other servers would ignore or reject them."""
    if not is_openrouter():
        return {}
    return {} if (settings.get('CHAT_REASONING') if reasoning is None else reasoning) else {'reasoning': {'enabled': False}}


def model_for(role='assistant'):
    """The model for a job. Roles with no model of their own use the assistant's."""
    return (settings.get(f'MODEL_{role.upper()}') if f'MODEL_{role.upper()}' in settings.SPECS else '') or settings.get('CHAT_MODEL')


def cached_tokens(usage):
    """Input tokens the provider served from its prompt cache (OpenAI and Anthropic report it the same way through OpenRouter)."""
    d = (usage or {}).get('prompt_tokens_details') or {}
    return d.get('cached_tokens') or (usage or {}).get('cache_read_input_tokens')


def with_cache(messages, model):
    """Prompt caching. OpenAI-style models cache an identical prompt prefix by themselves; Anthropic models need the prefix marked. The system message
    and the first user message (the task: a document, a page) are marked, unless a caller already split a message into parts with its own markers."""
    if 'anthropic/' not in (model or '') and 'claude' not in (model or ''):
        return messages
    out, marked = [], 0
    for i, m in enumerate(messages):
        if marked < 2 and m.get('role') in ('system', 'user') and isinstance(m.get('content'), str) and len(m['content']) > 2000 and i <= 1:
            m = {**m, 'content': [{'type': 'text', 'text': m['content'], 'cache_control': {'type': 'ephemeral'}}]}
            marked += 1
        out.append(m)
    return out


def chat(messages, role='assistant', max_tokens=1500, temperature=0.7, timeout=120):
    """One call to a chat model for a job. Returns the text; the call (model, time, tokens, cost) goes to the activity log of the current job or chat. Raises on failure."""
    model, t0 = model_for(role), time.perf_counter()
    try:
        r = httpx.post(f'{base_url()}/chat/completions', timeout=timeout, headers=headers(),
                       json={'model': model, 'temperature': temperature, 'max_tokens': max_tokens, 'messages': with_cache(messages, model), **extras()})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        activity.log('llm', model, ms=(time.perf_counter() - t0) * 1000, ok=False, role=role, error=f'{type(e).__name__}: {str(e)[:200]}')
        raise
    u = data.get('usage') or {}
    activity.log('llm', model, ms=(time.perf_counter() - t0) * 1000, tokens_in=u.get('prompt_tokens'), tokens_out=u.get('completion_tokens'), cost=u.get('cost'), role=role, cached=cached_tokens(u))
    return data['choices'][0]['message']['content'] or ''


def complete(system, user, max_tokens=1500, timeout=60, role='assistant'):
    """One plain call to a chat model. Raises on failure."""
    return chat([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], role=role, max_tokens=max_tokens, timeout=timeout)


OCR_PROMPT = ('Transcribe all the text in this image exactly as written, in reading order. Keep headings, lists and tables readable (tables as rows with " | "). '
              'Do not describe the image, do not add comments, and do not translate. If there is no text, answer with nothing.')


def ocr(image_png, mime='image/png'):
    """Text of an image through the vision model set for OCR (it must accept images). Used for scanned pages and pictures."""
    import base64
    data = f"data:{mime};base64,{base64.b64encode(image_png).decode()}"
    return chat([{'role': 'user', 'content': [{'type': 'text', 'text': OCR_PROMPT}, {'type': 'image_url', 'image_url': {'url': data}}]}], role='ocr', max_tokens=4000, temperature=0, timeout=180)


def test():
    """Ask the endpoint for one word. Returns {'ok', 'ms', 'model', 'answer' | 'error'}; never raises and never includes the key."""
    t0 = time.perf_counter()
    model = model_for('assistant')
    if not base_url():
        return {'ok': False, 'model': model, 'error': 'no address set'}
    try:
        r = httpx.post(f'{base_url()}/chat/completions', timeout=30, headers=headers(),
                       json={'model': model, 'max_tokens': 16, 'messages': [{'role': 'user', 'content': 'Reply with the single word: ready'}], **extras(False)})
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            return {'ok': False, 'ms': ms, 'model': model, 'error': f'{r.status_code}: {r.text[:200]}'}
        return {'ok': True, 'ms': ms, 'model': model, 'answer': (r.json()['choices'][0]['message']['content'] or '').strip()[:60]}
    except Exception as e:
        return {'ok': False, 'ms': round((time.perf_counter() - t0) * 1000), 'model': model, 'error': f'{type(e).__name__}: {str(e)[:200]}'}


def chat_tools(messages, tools, run_tool, role='assistant', max_rounds=8, max_tokens=6000, temperature=0, timeout=240):
    """A chat in which the model may call tools (OpenAI function format) until it answers. run_tool(name, args) -> str.
    Every model call and tool call goes to the activity log. Returns (final text, number of tool calls). On the last round tools are withdrawn so an answer is forced."""
    model, msgs, n_tools = model_for(role), list(messages), 0
    for rnd in range(max_rounds + 1):
        last = rnd == max_rounds
        if last:
            msgs.append({'role': 'user', 'content': 'You have used all your tool calls. Give your final answer now, from what you have.'})
        t0 = time.perf_counter()
        try:
            r = httpx.post(f'{base_url()}/chat/completions', timeout=timeout, headers=headers(),
                           json={'model': model, 'temperature': temperature, 'max_tokens': max_tokens, 'messages': with_cache(msgs, model), **({} if last else {'tools': tools}), **extras()})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            activity.log('llm', model, ms=(time.perf_counter() - t0) * 1000, ok=False, role=role, error=f'{type(e).__name__}: {str(e)[:200]}')
            raise
        msg = data['choices'][0]['message']
        u = data.get('usage') or {}
        calls = msg.get('tool_calls') or []
        activity.log('llm', model, ms=(time.perf_counter() - t0) * 1000, tokens_in=u.get('prompt_tokens'), tokens_out=u.get('completion_tokens'), cost=u.get('cost'), role=role,
                     tool_calls=[c['function']['name'] for c in calls] or None, cached=cached_tokens(u))
        if not calls or last:
            return msg.get('content') or '', n_tools
        msgs.append({'role': 'assistant', 'content': msg.get('content'), 'tool_calls': calls})
        for call in calls:
            n_tools += 1
            name, t1 = call['function']['name'], time.perf_counter()
            try:
                args = json.loads(call['function'].get('arguments') or '{}')
                result = str(run_tool(name, args))[:14000]
                ok = True
            except Exception as e:
                result, ok = json.dumps({'error': f'{type(e).__name__}: {str(e)[:200]}'}), False
            activity.log('tool', name, ms=(time.perf_counter() - t1) * 1000, ok=ok, chars=len(result))
            msgs.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
    return '', n_tools


def chat_stream(messages, role='assistant', max_tokens=2000, temperature=0.6, timeout=180):
    """A chat answer as it is written: yields the text in pieces. The call (model, time, tokens, cost) goes to the activity log when it ends. Raises on failure."""
    model, t0, first, usage = model_for(role), time.perf_counter(), None, {}
    body = {'model': model, 'temperature': temperature, 'max_tokens': max_tokens, 'messages': messages, 'stream': True,
            **({'usage': {'include': True}} if is_openrouter() else {'stream_options': {'include_usage': True}}), **extras()}
    try:
        with httpx.stream('POST', f'{base_url()}/chat/completions', json=body, headers=headers(), timeout=timeout) as r:
            if r.status_code != 200:
                raise RuntimeError(f'the model answered {r.status_code}: {r.read().decode()[:200]}')
            for line in r.iter_lines():
                if not line.startswith('data:') or line.strip() == 'data: [DONE]':
                    continue
                chunk = json.loads(line[5:])
                usage = chunk.get('usage') or usage
                piece = ((chunk.get('choices') or [{}])[0].get('delta') or {}).get('content')
                if piece:
                    first = first if first is not None else (time.perf_counter() - t0) * 1000
                    yield piece
    except GeneratorExit:
        activity.log('llm', model, ms=(time.perf_counter() - t0) * 1000, role=role, ok=True, note='the reader left before the end')
        raise
    except Exception as e:
        activity.log('llm', model, ms=(time.perf_counter() - t0) * 1000, ok=False, role=role, error=f'{type(e).__name__}: {str(e)[:200]}')
        raise
    activity.log('llm', model, ms=(time.perf_counter() - t0) * 1000, tokens_in=usage.get('prompt_tokens'), tokens_out=usage.get('completion_tokens'), cost=usage.get('cost'), role=role,
                 ttft_ms=None if first is None else int(first))

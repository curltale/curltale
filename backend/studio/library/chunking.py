"""Cut a document's text into passages that the embedding model can read whole. The size follows the model's input limit (EMBEDDING_MAX_TOKENS):
a passage longer than the model reads would be silently cut by it, and its tail could never be found."""
import re
from .. import config

CHARS_PER_TOKEN = 3.5                       # a cautious estimate for mixed text; figures, codes and other scripts use more tokens than plain English
MARKER = re.compile(r'^(Page \d+(?: \(OCR\))?|Slide \d+|Sheet: .+)$')


def tokens(text):
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def target_tokens():
    """About three quarters of what the embedding model reads, leaving room for estimate error."""
    return max(64, int(config.EMBEDDING_MAX_TOKENS * 0.75))


def _split_long(paragraph, limit):
    """A paragraph longer than the limit is cut at sentence ends, then at spaces."""
    if tokens(paragraph) <= limit:
        return [paragraph]
    out, cur = [], ''
    for piece in re.split(r'(?<=[.!?;])\s+', paragraph):
        while tokens(piece) > limit:                                    # no sentence end in reach: cut at a space
            cut = piece.rfind(' ', 0, int(limit * CHARS_PER_TOKEN)) or int(limit * CHARS_PER_TOKEN)
            cut = cut if cut > 0 else int(limit * CHARS_PER_TOKEN)
            if cur:
                out.append(cur); cur = ''
            out.append(piece[:cut].strip()); piece = piece[cut:].strip()
        if cur and tokens(cur + ' ' + piece) > limit:
            out.append(cur); cur = piece
        else:
            cur = (cur + ' ' + piece).strip()
    if cur:
        out.append(cur)
    return [o for o in out if o]


def chunk(text, target=None, overlap=None):
    """-> [{'idx', 'text', 'tokens', 'char_start', 'char_end', 'heading_path', 'location'}]. Paragraph based; a new chunk starts at a page, slide or sheet marker."""
    target = target or target_tokens()
    overlap = target // 8 if overlap is None else overlap
    paras = []                                                          # (text, char offset)
    pos = 0
    for raw in re.split(r'\n{2,}', text):
        p = raw.strip()
        if p:
            at = text.find(p, pos)
            at = at if at >= 0 else pos
            for part in _split_long(p, target):
                paras.append((part, at))
            pos = at + len(p)
    chunks, cur, cur_tok = [], [], 0
    headings, location = [], None
    start_head, start_loc, start_at = [], None, 0
    has_body = False

    def flush(keep_overlap=True):
        nonlocal cur, cur_tok, has_body
        if not cur:
            return
        body = '\n\n'.join(p for p, _ in cur)
        chunks.append({'idx': len(chunks), 'text': body, 'tokens': tokens(body), 'char_start': cur[0][1], 'char_end': cur[-1][1] + len(cur[-1][0]),
                       'heading_path': list(start_head), 'location': start_loc})
        tail, total = [], 0
        if keep_overlap and overlap:
            for item in reversed(cur):
                if MARKER.match(item[0].split('\n', 1)[0]):
                    break
                tail.insert(0, item); total += tokens(item[0])
                if total >= overlap:
                    break
        cur, cur_tok = (tail, total) if total < target else ([], 0)
        has_body = bool(cur)

    for para, at in paras:
        first = para.split('\n', 1)[0]
        if MARKER.match(first):
            flush(keep_overlap=False)
            location = first
        h = re.match(r'^(#{1,6})\s+(.+?)\s*#*$', first)
        if h:
            del headings[len(h.group(1)) - 1:]
            headings.append(h.group(2).strip())
        t = tokens(para)
        if cur and cur_tok + t > target:
            flush()
            if cur and cur_tok + t > target:                                 # no room for the overlap and this paragraph together: the paragraph wins
                cur, cur_tok, has_body = [], 0, False
        if not cur:
            start_head, start_loc, start_at = list(headings), location, at
        cur.append((para, at)); cur_tok += t
        if not has_body:
            if not (h or MARKER.match(first)):
                has_body = True                                              # a chunk is placed where its first real text is, not at a heading or page label before it
            else:
                start_head, start_loc = list(headings), location
    flush(keep_overlap=False)
    return chunks

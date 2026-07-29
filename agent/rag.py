"""
v0.4.1 RAG — faiss-cpu + sqlite FTS5 + jieba + Python RRF (零运维, 同进程)

设计 (替换之前 ES 8 方案, 治本 ES 装不下 / OOM / 端口问题):
  - 向量: faiss-cpu IndexFlatIP (内积, 跟 cosine 等效, 反正 vec 都 L2 normalized)
  - 中文分词: jieba (精准模式)
  - BM25: sqlite FTS5 (virtual table, Porter stemmer, unicode61 token)
  - 混合: BM25 (sqlite FTS5) + knn (faiss) + Python RRF 融合 (跟 ES RRF 等效)
  - 持久化:
    - sqlite kb_entries (source of truth) + kb_entries_fts (FTS5 虚表)
    - data/faiss.index (faiss 向量索引, 二进制, 加载 < 100ms)
    - data/faiss_ids.json (id 跟 faiss idx 映射)
  - 全部同 chat-bi-mavis 进程, 1G 内存够, 零 systemd 运维

依赖: faiss-cpu, jieba, numpy
"""
import os
import json
import time
import logging
import threading
from typing import List, Dict, Optional, Any, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ============================================================
# Config
# ============================================================
ES_URL = os.environ.get("CHAT_BI_ES_URL", "")  # 兼容老 config, 空 = 不走 ES
EMBED_MODEL_DIR = os.environ.get(
    "CHAT_BI_EMBED_DIR",
    os.path.join(os.path.dirname(__file__), "..", "models", "bge-small-zh-v1.5"),
)
EMBED_DIMS = 512
EMBED_MAX_LEN = 512

# faiss 索引路径 (跟 sqlite KB 同目录)
KB_DIR = os.environ.get(
    "DATA_ANALYST_KB_DIR",
    os.path.join(os.path.dirname(__file__), "..", "data"),
)
FAISS_INDEX_PATH = os.path.join(KB_DIR, "faiss.index")
FAISS_IDS_PATH = os.path.join(KB_DIR, "faiss_ids.json")


# ============================================================
# Embedding 模型 (bge-small-zh-v1.5 ONNX, 懒加载)
# ============================================================
_embed_session = None
_embed_tokenizer = None
_embed_lock = threading.Lock()


def _load_embed_model():
    """懒加载 bge ONNX 模型 (单例, 线程安全)"""
    global _embed_session, _embed_tokenizer
    if _embed_session is not None:
        return _embed_session, _embed_tokenizer
    with _embed_lock:
        if _embed_session is not None:
            return _embed_session, _embed_tokenizer
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            logger.error(f"onnxruntime/tokenizers 未装: {e}")
            return None, None

        model_path = os.path.join(EMBED_MODEL_DIR, "model.onnx")
        tok_path = os.path.join(EMBED_MODEL_DIR, "tokenizer.json")
        if not os.path.exists(model_path) or not os.path.exists(tok_path):
            logger.error(f"bge 模型文件不存在: {model_path} / {tok_path}")
            return None, None

        try:
            tok = Tokenizer.from_file(tok_path)
            tok.enable_padding(pad_id=0, pad_token="[PAD]", length=EMBED_MAX_LEN)
            tok.enable_truncation(max_length=EMBED_MAX_LEN)
            sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            _embed_session = sess
            _embed_tokenizer = tok
            logger.info(f"bge-small-zh-v1.5 ONNX 加载成功, 路径={EMBED_MODEL_DIR}")
            return sess, tok
        except Exception as e:
            logger.error(f"bge 加载失败: {e}")
            return None, None


def _mean_pool(last_hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """BGE 标准 mean pooling (mask 加权平均)"""
    m = mask[:, :, None].astype(np.float32)
    summed = (last_hidden * m).sum(axis=1)
    counts = m.sum(axis=1).clip(min=1e-9)
    pooled = summed / counts
    norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
    return (pooled / norms).astype(np.float32)


def embed_texts(texts: List[str]) -> Optional[np.ndarray]:
    """批量 embed → (N, 512) L2 normalized 矩阵"""
    if not texts:
        return np.zeros((0, EMBED_DIMS), dtype=np.float32)
    sess, tok = _load_embed_model()
    if sess is None or tok is None:
        return None
    try:
        encodings = tok.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)
        outputs = sess.run(None, {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        })
        last_hidden = outputs[0]
        return _mean_pool(last_hidden, attention_mask)
    except Exception as e:
        logger.error(f"embed 失败: {e}")
        return None


def embed_text(text: str) -> Optional[List[float]]:
    """单条 embed → 512 维 list[float]"""
    arr = embed_texts([text])
    if arr is None or len(arr) == 0:
        return None
    return arr[0].tolist()


# ============================================================
# 中文分词 (jieba, 给 FTS5 用)
# ============================================================
_jieba_initialized = False


def _tokenize_chinese(text: str) -> str:
    """jieba 精准分词, 用空格 join (FTS5 需要空格分词)"""
    global _jieba_initialized
    if not _jieba_initialized:
        try:
            import jieba
            jieba.setLogLevel(logging.WARNING)  # 静默 init log
            # 强制触发初始化 (第一次调用才 init, 在 thread 里会卡)
            jieba.lcut("init")
            _jieba_initialized = True
        except ImportError:
            logger.warning("jieba 未装, 用空格 split 降级")
            return text
    try:
        import jieba
        tokens = jieba.lcut_for_search(text)  # 搜索模式: 切得更细, 召回更好
        # 过滤: 1 字符 + 纯标点 + 纯数字 (BM25 不友好)
        out = []
        for t in tokens:
            t = t.strip()
            if len(t) < 2:
                continue
            if not any('\u4e00' <= c <= '\u9fff' for c in t):  # 必须含至少 1 个中文
                continue
            out.append(t)
        return " ".join(out)
    except Exception as e:
        logger.warning(f"jieba 分词失败: {e}")
        return text


# ============================================================
# Faiss 索引管理 (单例, 加锁)
# ============================================================
_faiss_index = None
_faiss_ids: List[int] = []  # faiss idx → KB id 映射
_faiss_lock = threading.RLock()


def _load_faiss_index() -> bool:
    """加载或初始化 faiss 索引 (线程安全)"""
    global _faiss_index, _faiss_ids
    if _faiss_index is not None:
        return True
    with _faiss_lock:
        if _faiss_index is not None:
            return True
        try:
            import faiss
        except ImportError:
            logger.error("faiss-cpu 未装")
            return False
        os.makedirs(KB_DIR, exist_ok=True)
        if os.path.exists(FAISS_INDEX_PATH) and os.path.exists(FAISS_IDS_PATH):
            try:
                _faiss_index = faiss.read_index(FAISS_INDEX_PATH)
                with open(FAISS_IDS_PATH, "r", encoding="utf-8") as f:
                    _faiss_ids = json.load(f)
                logger.info(f"faiss 索引加载成功, {len(_faiss_ids)} 条")
                return True
            except Exception as e:
                logger.error(f"faiss 索引加载失败, 重建: {e}")
        # 初始化空索引
        _faiss_index = faiss.IndexFlatIP(EMBED_DIMS)
        _faiss_ids = []
        return True


def _save_faiss_index():
    """持久化 (加锁)"""
    with _faiss_lock:
        try:
            import faiss
            faiss.write_index(_faiss_index, FAISS_INDEX_PATH)
            with open(FAISS_IDS_PATH, "w", encoding="utf-8") as f:
                json.dump(_faiss_ids, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"faiss 索引保存失败: {e}")


# ============================================================
# CRUD
# ============================================================
def index_kb_entry(entry: Dict[str, Any]) -> bool:
    """
    写 faiss 索引 + (调用方写 sqlite + FTS5).
    注意: sqlite 跟 FTS5 由 knowledge_base.py 管理, 这里只管 faiss.
    """
    if not _load_faiss_index():
        return False
    entry_id = entry.get("id")
    if entry_id is None:
        return False
    title = entry.get("title", "") or ""
    content = entry.get("content", "") or ""
    title_vec = embed_text(title)
    content_vec = embed_text(content[:1000])
    if title_vec is None and content_vec is None:
        return False
    # 串成 (1, 512) — 用 content_vec 为主, title 跟 content 拼一起再 embed 也行
    # 实际: 用 content 完整 emb, 同时把 title 加权 (1.5x 重要)
    if title_vec is None:
        title_vec = [0.0] * EMBED_DIMS
    if content_vec is None:
        content_vec = [0.0] * EMBED_DIMS
    # 加权: title 1.5x
    combined = (np.array(title_vec) * 1.5 + np.array(content_vec)) / 2.5
    norm = np.linalg.norm(combined)
    if norm > 0:
        combined = combined / norm
    vec = combined.astype(np.float32).reshape(1, EMBED_DIMS)

    with _faiss_lock:
        import faiss
        # 检查是否已存在
        if entry_id in _faiss_ids:
            # 删旧的
            old_idx = _faiss_ids.index(entry_id)
            _faiss_index.remove_ids(np.array([old_idx], dtype=np.int64))
            _faiss_ids.pop(old_idx)
        _faiss_index.add(vec)
        _faiss_ids.append(entry_id)
        _save_faiss_index()
    return True


def delete_kb_entry(entry_id: int) -> bool:
    if not _load_faiss_index():
        return False
    with _faiss_lock:
        if entry_id not in _faiss_ids:
            return True  # 不存在也算成功
        old_idx = _faiss_ids.index(entry_id)
        _faiss_index.remove_ids(np.array([old_idx], dtype=np.int64))
        _faiss_ids.pop(old_idx)
        _save_faiss_index()
    return True


def reindex_all(entries: List[Dict[str, Any]]) -> Tuple[int, int]:
    """全量重建 faiss 索引 (从 sqlite 灌)"""
    global _faiss_index, _faiss_ids
    if not _load_faiss_index():
        return 0, 0
    with _faiss_lock:
        import faiss
        # 清空
        _faiss_index = faiss.IndexFlatIP(EMBED_DIMS)
        _faiss_ids = []
    ok, fail = 0, 0
    for e in entries:
        if index_kb_entry(e):
            ok += 1
        else:
            fail += 1
    return ok, fail


# ============================================================
# 检索
# ============================================================
def bm25_search(query: str, top_k: int = 20, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    BM25 检索 (走 sqlite FTS5).
    调用方传 KnowledgeBase 引用 (避免循环 import).
    """
    from .knowledge_base import get_kb
    kb = get_kb()
    return kb.search_fts5(query, limit=top_k, category=category)


def knn_search(query_vec: List[float], top_k: int = 20, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """faiss 向量检索, 返 (id, score) 列表"""
    if not _load_faiss_index():
        return []
    if not _faiss_ids:
        return []
    import faiss
    vec = np.array([query_vec], dtype=np.float32)
    # 搜
    scores, idxs = _faiss_index.search(vec, min(top_k, len(_faiss_ids)))
    results: List[Dict[str, Any]] = []
    for score, idx in zip(scores[0], idxs[0]):
        if idx < 0 or idx >= len(_faiss_ids):
            continue
        kb_id = _faiss_ids[idx]
        results.append({"id": kb_id, "score": float(score)})
    # 跟 sqlite join 拿 entry
    if results:
        from .knowledge_base import get_kb
        kb = get_kb()
        for r in results:
            entry = kb.get(r["id"])
            if entry:
                r.update({
                    "category": entry.category,
                    "title": entry.title,
                    "content": entry.content,
                    "source": entry.source,
                    "tags": entry.tags,
                    "confidence": entry.confidence,
                    "created_at": entry.created_at,
                })
                # category 过滤
                if category and entry.category != category:
                    r["_filter_out"] = True
        results = [r for r in results if not r.get("_filter_out")]
    return results


def hybrid_search(
    query: str,
    top_k: int = 5,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    混合检索: BM25 (sqlite FTS5) + faiss knn + Python RRF 融合
    (跟之前 ES RRF 等效, 不用 enterprise license)
    """
    if not query or not query.strip():
        return []
    RRF_K = 60
    N = max(top_k * 4, 20)  # 每个 ranker 取 N 条, 留给 RRF 融合

    # ranker 1: BM25 (FTS5)
    bm25_hits = bm25_search(query, top_k=N, category=category)
    # ranker 2: faiss knn
    query_vec = embed_text(query)
    knn_hits = []
    if query_vec is not None:
        knn_hits = knn_search(query_vec, top_k=N, category=category)

    # 端: fallback
    if not bm25_hits:
        return knn_hits[:top_k]
    if not knn_hits:
        return bm25_hits[:top_k]

    # 算 RRF 分数
    rrf_scores: Dict[int, float] = {}
    entry_cache: Dict[int, Dict[str, Any]] = {}

    for rank, e in enumerate(bm25_hits):
        key = e["id"]
        rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        entry_cache[key] = e

    for rank, e in enumerate(knn_hits):
        key = e["id"]
        rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        if key not in entry_cache:
            entry_cache[key] = e

    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)
    out: List[Dict[str, Any]] = []
    for k in sorted_keys[:top_k]:
        e = dict(entry_cache[k])
        e["score"] = round(rrf_scores[k], 6)
        out.append(e)
    return out


# ============================================================
# 给 LLM 用的 RAG 召回
# ============================================================
def recall_for_context(query: str, limit: int = 5) -> str:
    """给 LLM 拼 markdown 段 (跟 sqlite 的 recall_for_context 兼容)"""
    if not query or not query.strip():
        return ""
    results = hybrid_search(query, top_k=limit)
    if not results:
        return ""
    lines = [f"## 📚 知识库相关历史 (faiss + FTS5 混合检索 · {len(results)} 条)", ""]
    for i, e in enumerate(results, 1):
        tag_str = ", ".join((e.get("tags") or [])[:5]) or "—"
        content_short = e.get("content", "")
        if len(content_short) > 400:
            content_short = content_short[:400] + "..."
        score_str = f" | RRF 分数: {e.get('score', 0):.2f}" if e.get("score") is not None else ""
        lines.append(
            f"### {i}. [{e.get('category', '')}] {e.get('title', '')}\n"
            f"- 来源: `{e.get('source', '')}` | 置信度: {e.get('confidence', 0):.2f}{score_str} | 标签: {tag_str}\n"
            f"- 内容: {content_short}\n"
        )
    return "\n".join(lines)


# ============================================================
# 列表/统计/详情 (跟 sqlite 接口兼容, 给 server.py 用)
# ============================================================
def get_stats() -> Dict[str, Any]:
    """总览: total / by_category / by_source / avg_confidence + faiss 状态"""
    from .knowledge_base import get_kb
    kb = get_kb()
    stats = kb.get_stats()
    # 加 faiss 状态
    if _load_faiss_index():
        with _faiss_lock:
            stats["faiss"] = {
                "indexed": len(_faiss_ids),
                "in_sync": len(_faiss_ids) == stats.get("total", 0),
                "index_path": FAISS_INDEX_PATH,
            }
    stats["backend"] = "faiss-cpu + FTS5"
    return stats


def get_categories() -> List[str]:
    from .knowledge_base import get_kb
    return get_kb().get_categories()


def get_entry(entry_id: int) -> Optional[Dict[str, Any]]:
    from .knowledge_base import get_kb
    entry = get_kb().get(entry_id)
    if not entry:
        return None
    return {
        "id": entry.id, "category": entry.category, "title": entry.title,
        "content": entry.content, "source": entry.source, "tags": entry.tags,
        "confidence": entry.confidence, "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }


def list_recent(limit: int = 20, category: Optional[str] = None) -> List[Dict[str, Any]]:
    from .knowledge_base import get_kb
    entries = get_kb().list_recent(limit=limit, category=category)
    return [
        {
            "id": e.id, "category": e.category, "title": e.title,
            "content": e.content, "source": e.source, "tags": e.tags,
            "confidence": e.confidence, "created_at": e.created_at,
            "updated_at": e.updated_at,
        }
        for e in entries
    ]


def search_text(query: str, limit: int = 10, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """UI 搜索框用, 走 FTS5 (BM25) — 跟 sqlite 兼容"""
    from .knowledge_base import get_kb
    entries = get_kb().search_fts5(query, limit=limit, category=category)
    return [
        {
            "id": e["id"], "category": e["category"], "title": e["title"],
            "content": e["content"], "source": e["source"], "tags": e["tags"],
            "confidence": e["confidence"], "created_at": e["created_at"],
        }
        for e in entries
    ]


# ============================================================
# 健康检查
# ============================================================
def is_available() -> bool:
    """faiss + 模型 + FTS5 都 OK?"""
    try:
        sess, _ = _load_embed_model()
        if sess is None:
            return False
        if not _load_faiss_index():
            return False
        # FTS5 测一下
        from .knowledge_base import get_kb
        kb = get_kb()
        # 任意查, 看 fts5 表存在
        with kb._connect() as conn:
            cur = conn.execute("SELECT 1 FROM kb_entries_fts LIMIT 1")
            cur.fetchone()
        return True
    except Exception as e:
        logger.warning(f"is_available 检查失败: {e}")
        return False

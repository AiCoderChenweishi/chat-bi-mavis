"""
v0.4 RAG — ES 8 混合检索 (smartcn BM25 + bge 向量 + RRF 融合)

设计:
  - 存储: Elasticsearch 8.18 (sandbox / 8.153 自部署)
  - 向量: bge-small-zh-v1.5 ONNX (512 维, Xenova 转)
  - 中文分词: smartcn (ES 自带插件)
  - 混合检索: BM25 (knn 之前默认) + knn (dense_vector) + RRF 融合 (ES 8.8+ native)
  - sqlite 保留作 backup (跟 ES 双写)

依赖: onnxruntime, tokenizers, numpy (已装)
"""
import os
import json
import time
import logging
import threading
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Tuple, Any

import numpy as np

logger = logging.getLogger(__name__)

# ============================================================
# Config
# ============================================================
ES_URL = os.environ.get("CHAT_BI_ES_URL", "http://127.0.0.1:9200")
ES_INDEX = os.environ.get("CHAT_BI_ES_INDEX", "chat-bi-kb")
ES_USER = os.environ.get("CHAT_BI_ES_USER", "")  # 空 = 无 auth
ES_PASS = os.environ.get("CHAT_BI_ES_PASS", "")

EMBED_MODEL_DIR = os.environ.get(
    "CHAT_BI_EMBED_DIR",
    os.path.join(os.path.dirname(__file__), "..", "models", "bge-small-zh-v1.5"),
)
EMBED_DIMS = 512
EMBED_MAX_LEN = 512


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
# ES HTTP 客户端 (免 elasticsearch 依赖, 走 urllib)
# ============================================================
def _es_request(method: str, path: str, body: Optional[dict] = None, timeout: int = 10) -> Tuple[int, dict]:
    """发 ES HTTP 请求, 返 (status_code, json_body)"""
    url = f"{ES_URL}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if ES_USER and ES_PASS:
        import base64
        auth = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"raw": raw, "error": str(e)}
    except Exception as e:
        logger.error(f"ES 请求失败 {method} {url}: {e}")
        return 0, {"error": str(e)}


# ============================================================
# 索引管理
# ============================================================
def ensure_index() -> bool:
    """确保 chat-bi-kb 索引存在 (没有就建)"""
    code, body = _es_request("GET", f"/{ES_INDEX}")
    if code == 200:
        return True
    if code == 404:
        # 建索引
        mapping = {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            },
            "mappings": {
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "keyword"},
                    "title": {"type": "text", "analyzer": "smartcn", "search_analyzer": "smartcn"},
                    "content": {"type": "text", "analyzer": "smartcn", "search_analyzer": "smartcn"},
                    "source": {"type": "keyword"},
                    "tags": {"type": "keyword"},
                    "confidence": {"type": "float"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "title_vector": {"type": "dense_vector", "dims": EMBED_DIMS, "index": True, "similarity": "cosine"},
                    "content_vector": {"type": "dense_vector", "dims": EMBED_DIMS, "index": True, "similarity": "cosine"},
                }
            }
        }
        code, body = _es_request("PUT", f"/{ES_INDEX}", body=mapping)
        if code in (200, 201):
            logger.info(f"chat-bi-kb 索引建成功")
            return True
        logger.error(f"建索引失败: {code} {body}")
        return False
    logger.error(f"检查索引失败: {code} {body}")
    return False


# ============================================================
# CRUD
# ============================================================
def index_kb_entry(entry: Dict[str, Any]) -> bool:
    """把 KB entry 写 ES (1 条), title + content 都 embed"""
    entry_id = entry.get("id")
    if entry_id is None:
        logger.error("entry 缺 id")
        return False
    title = entry.get("title", "") or ""
    content = entry.get("content", "") or ""
    # embed (title 跟 content 各一次, 让两种查询都能命中)
    title_vec = embed_text(title) or [0.0] * EMBED_DIMS
    content_vec = embed_text(content[:1000]) or [0.0] * EMBED_DIMS  # 截断避免超长
    doc = {
        "id": entry_id,
        "category": entry.get("category", "洞察"),
        "title": title,
        "content": content,
        "source": entry.get("source", "manual"),
        "tags": entry.get("tags", []),
        "confidence": entry.get("confidence", 0.8),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "title_vector": title_vec,
        "content_vector": content_vec,
    }
    # refresh=wait_for 让后续 search 立刻可见 (KB 量小可接受)
    code, body = _es_request("PUT", f"/{ES_INDEX}/_doc/{entry_id}?refresh=wait_for", body=doc, timeout=15)
    if code in (200, 201):
        logger.info(f"KB #{entry_id} 写 ES 成功")
        return True
    logger.error(f"KB #{entry_id} 写 ES 失败: {code} {body}")
    return False


def delete_kb_entry(entry_id: int) -> bool:
    code, body = _es_request("DELETE", f"/{ES_INDEX}/_doc/{entry_id}?refresh=wait_for", timeout=10)
    if code in (200, 201, 404):
        return True
    logger.error(f"删 KB #{entry_id} 失败: {code} {body}")
    return False


def reindex_all(entries: List[Dict[str, Any]]) -> Tuple[int, int]:
    """从 sqlite 灌 ES (全量重灌, 用于首次启动 + sqlite 跟 ES 不一致)"""
    ok, fail = 0, 0
    for e in entries:
        if index_kb_entry(e):
            ok += 1
        else:
            fail += 1
    return ok, fail


# ============================================================
# 检索 — 混合 (BM25 smartcn + knn 向量 + RRF 融合)
# ============================================================
def hybrid_search(
    query: str,
    top_k: int = 5,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    混合检索: BM25 (smartcn) + knn 向量, Python 端 RRF 融合
    (ES native rank.rrf 是 enterprise license 限定, basic license 用不了)

    RRF 公式: rrf_score(d) = sum( 1 / (rank_constant + rank_i(d)) ) for each ranker
    """
    if not query or not query.strip():
        return []
    RRF_K = 60  # rank_constant (跟 ES RRF 默认一致)
    N = max(top_k * 4, 20)  # 每个 ranker 取 N 条, 留给 RRF 融合 (召回池放大)

    # ranker 1: BM25 (smartcn)
    bm25_hits = _search_bm25(query, N, category)
    # ranker 2: knn 向量 (content_vector)
    knn_hits = []
    query_vec = embed_text(query)
    if query_vec is not None:
        knn_hits = _search_knn(query_vec, N, category)

    # 各自按 ES 返的 _score 排序, 取 rank
    bm25_ranked = sorted(bm25_hits, key=lambda x: x.get("score", 0), reverse=True)
    knn_ranked = sorted(knn_hits, key=lambda x: x.get("score", 0), reverse=True)

    # 端: fallback (任何一个 list 空, 退化为另一个)
    if not bm25_ranked:
        return knn_ranked[:top_k]
    if not knn_ranked:
        return bm25_ranked[:top_k]

    # 算 RRF 分数
    rrf_scores: Dict[Any, float] = {}
    entry_cache: Dict[Any, Dict[str, Any]] = {}

    for rank, e in enumerate(bm25_ranked):
        key = e["id"]
        rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        entry_cache[key] = e

    for rank, e in enumerate(knn_ranked):
        key = e["id"]
        rrf_scores[key] = rrf_scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        entry_cache[key] = e

    # 按 RRF 分数排序
    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)
    out: List[Dict[str, Any]] = []
    for k in sorted_keys[:top_k]:
        e = dict(entry_cache[k])
        e["score"] = round(rrf_scores[k], 6)
        out.append(e)
    return out


def _search_bm25(query: str, size: int, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """BM25 子检索, 返 _score 原始分数"""
    body: Dict[str, Any] = {
        "size": size,
        "query": {
            "multi_match": {
                "query": query,
                "fields": ["title^2", "content"],
                "type": "best_fields",
                "analyzer": "smartcn",
            }
        },
    }
    if category:
        body["query"] = {"bool": {"must": [body["query"]], "filter": [{"term": {"category": category}}]}}
    code, resp = _es_request("POST", f"/{ES_INDEX}/_search", body=body, timeout=10)
    if code != 200:
        return []
    return [_hit_to_entry(h) for h in resp.get("hits", {}).get("hits", [])]


def _search_knn(query_vec: List[float], size: int, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """knn 子检索, 返 _score (cosine similarity)"""
    knn_clause: Dict[str, Any] = {
        "field": "content_vector",
        "query_vector": query_vec,
        "k": size,
        "num_candidates": max(size * 4, 50),
    }
    body: Dict[str, Any] = {
        "size": size,
        "knn": knn_clause,
    }
    if category:
        body["knn"]["filter"] = [{"term": {"category": category}}]
    code, resp = _es_request("POST", f"/{ES_INDEX}/_search", body=body, timeout=10)
    if code != 200:
        logger.warning(f"knn 检索失败: {code} {resp}")
        return []
    return [_hit_to_entry(h) for h in resp.get("hits", {}).get("hits", [])]


def bm25_only_search(query: str, top_k: int = 5, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """降级: embed 失败时走纯 BM25"""
    body: Dict[str, Any] = {
        "size": top_k,
        "query": {
            "multi_match": {
                "query": query,
                "fields": ["title^2", "content"],
                "type": "best_fields",
                "analyzer": "smartcn",
            }
        },
    }
    if category:
        body["query"] = {"bool": {"must": [body["query"]], "filter": [{"term": {"category": category}}]}}
    code, resp = _es_request("POST", f"/{ES_INDEX}/_search", body=body, timeout=10)
    if code != 200:
        return []
    hits = resp.get("hits", {}).get("hits", [])
    return [_hit_to_entry(h) for h in hits]


def _hit_to_entry(hit: Dict[str, Any]) -> Dict[str, Any]:
    src = hit.get("_source", {})
    return {
        "id": src.get("id", hit.get("_id")),
        "category": src.get("category", ""),
        "title": src.get("title", ""),
        "content": src.get("content", ""),
        "source": src.get("source", ""),
        "tags": src.get("tags", []),
        "confidence": src.get("confidence", 0.0),
        "created_at": src.get("created_at"),
        "score": hit.get("_score"),
    }


# ============================================================
# 跟 sqlite 兼容的列表/统计 (UI 用, 跟 ES 走)
# ============================================================
def list_recent(limit: int = 20, category: Optional[str] = None) -> List[Dict[str, Any]]:
    body: Dict[str, Any] = {
        "size": limit,
        "sort": [{"updated_at": {"order": "desc", "missing": "_last"}}],
    }
    if category:
        body["query"] = {"term": {"category": category}}
    code, resp = _es_request("POST", f"/{ES_INDEX}/_search", body=body, timeout=10)
    if code != 200:
        return []
    hits = resp.get("hits", {}).get("hits", [])
    return [_hit_to_entry(h) for h in hits]


def search_text(query: str, limit: int = 10, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """跟 sqlite 的 search() 接口一致 — UI 搜索框用, 走 BM25 (快 + 简单)"""
    return bm25_only_search(query, limit, category)


def get_entry(entry_id: int) -> Optional[Dict[str, Any]]:
    code, resp = _es_request("GET", f"/{ES_INDEX}/_doc/{entry_id}")
    if code != 200 or not resp.get("found"):
        return None
    src = resp.get("_source", {})
    return {
        "id": src.get("id", entry_id),
        "category": src.get("category", ""),
        "title": src.get("title", ""),
        "content": src.get("content", ""),
        "source": src.get("source", ""),
        "tags": src.get("tags", []),
        "confidence": src.get("confidence", 0.0),
        "created_at": src.get("created_at"),
        "updated_at": src.get("updated_at"),
    }


def get_stats() -> Dict[str, Any]:
    """总览: total / by_category / by_source / avg_confidence"""
    code, resp = _es_request("POST", f"/{ES_INDEX}/_search", body={
        "size": 0,
        "aggs": {
            "by_category": {"terms": {"field": "category", "size": 20}},
            "by_source": {"terms": {"field": "source", "size": 20}},
            "avg_conf": {"avg": {"field": "confidence"}},
        }
    }, timeout=10)
    total = resp.get("hits", {}).get("total", {}).get("value", 0) if code == 200 else 0
    by_cat = {b["key"]: b["doc_count"] for b in (resp.get("aggregations", {}).get("by_category", {}).get("buckets", []) if code == 200 else [])}
    by_src = {b["key"]: b["doc_count"] for b in (resp.get("aggregations", {}).get("by_source", {}).get("buckets", []) if code == 200 else [])}
    avg_conf = round(resp.get("aggregations", {}).get("avg_conf", {}).get("value", 0) or 0, 3) if code == 200 else 0
    return {
        "total": total,
        "by_category": by_cat,
        "by_source": by_src,
        "avg_confidence": avg_conf,
        "backend": "elasticsearch",
        "es_url": ES_URL,
    }


def get_categories() -> List[str]:
    code, resp = _es_request("POST", f"/{ES_INDEX}/_search", body={
        "size": 0,
        "aggs": {"by_category": {"terms": {"field": "category", "size": 20}}}
    }, timeout=10)
    if code != 200:
        return []
    return [b["key"] for b in resp.get("aggregations", {}).get("by_category", {}).get("buckets", [])]


# ============================================================
# 给 LLM 用的 RAG 召回 (跟 sqlite 的 recall_for_context 兼容)
# ============================================================
def recall_for_context(query: str, limit: int = 5) -> str:
    """给 LLM 拼 markdown 段 (跟 knowledge_base.py 的同名函数兼容)"""
    if not query or not query.strip():
        return ""
    results = hybrid_search(query, top_k=limit)
    if not results:
        return ""
    lines = [f"## 📚 知识库相关历史 (ES 混合检索 · {len(results)} 条)", ""]
    for i, e in enumerate(results, 1):
        tag_str = ", ".join((e.get("tags") or [])[:5]) or "—"
        content_short = e.get("content", "")[:400] + ("..." if len(e.get("content", "")) > 400 else "")
        score_str = f" | RRF 分数: {e.get('score', 0):.2f}" if e.get("score") is not None else ""
        lines.append(
            f"### {i}. [{e.get('category', '')}] {e.get('title', '')}\n"
            f"- 来源: `{e.get('source', '')}` | 置信度: {e.get('confidence', 0):.2f}{score_str} | 标签: {tag_str}\n"
            f"- 内容: {content_short}\n"
        )
    return "\n".join(lines)


# ============================================================
# 健康检查
# ============================================================
def is_available() -> bool:
    """ES + 模型都可用? 任何一边 down 都返 False (走 sqlite 兜底)"""
    code, _ = _es_request("GET", "/_cluster/health", timeout=3)
    if code != 200:
        return False
    sess, _ = _load_embed_model()
    return sess is not None

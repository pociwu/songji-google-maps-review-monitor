"""Local, restart-safe review similarity analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import unicodedata
from typing import Any, Callable, Protocol

import yaml

from .config import Settings
from .database import Database


LOG = logging.getLogger(__name__)
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
KEEP_RE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)


class EmbeddingProvider(Protocol):
    version: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class E5OnnxProvider:
    """Run the repository's ONNX graph directly, without loading PyTorch."""

    def __init__(self, model_name: str):
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
        if hasattr(os, "getpriority") and hasattr(os, "nice"):
            try:
                current_nice = os.getpriority(os.PRIO_PROCESS, 0)
                if current_nice < 10:
                    os.nice(10 - current_nice)
            except OSError:
                LOG.debug("無法降低分析程序優先序", exc_info=True)
        try:
            import numpy as np
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                "缺少本機分析套件；請執行 pip install -e '.[analysis]'"
            ) from exc
        try:
            model_path = hf_hub_download(model_name, "onnx/model.onnx")
            tokenizer_path = hf_hub_download(model_name, "onnx/tokenizer.json")
            self.tokenizer = Tokenizer.from_file(tokenizer_path)
            self.tokenizer.enable_truncation(max_length=512)
            self.tokenizer.enable_padding(
                pad_id=self.tokenizer.token_to_id("<pad>") or 1,
                pad_token="<pad>",
            )
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(
                model_path,
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self.input_names = {item.name for item in self.session.get_inputs()}
            self.np = np
        except Exception as exc:
            raise RuntimeError(f"無法載入本機 ONNX 模型 {model_name}：{exc}") from exc
        self.version = f"{model_name}:onnx"

    def encode(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for start in range(0, len(texts), 8):
            encodings = self.tokenizer.encode_batch(
                [f"query: {text}" for text in texts[start : start + 8]]
            )
            ids = self.np.asarray([item.ids for item in encodings], dtype=self.np.int64)
            mask = self.np.asarray(
                [item.attention_mask for item in encodings], dtype=self.np.int64
            )
            inputs = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                inputs["token_type_ids"] = self.np.zeros_like(ids)
            output = self.session.run(None, inputs)[0]
            if output.ndim == 3:
                expanded = mask[..., None].astype(output.dtype)
                output = (output * expanded).sum(axis=1) / expanded.sum(axis=1).clip(min=1)
            norms = self.np.linalg.norm(output, axis=1, keepdims=True).clip(min=1e-12)
            result.extend((output / norms).tolist())
        return result


@dataclass(slots=True)
class AnalysisRules:
    excluded_reviews: dict[tuple[str, str], str]
    excluded_groups: dict[str, str]
    common_phrases: list[str]


def load_rules(path: Path) -> AnalysisRules:
    if not path.exists():
        return AnalysisRules({}, {}, [])
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def reasoned(name: str) -> list[dict[str, Any]]:
        values = raw.get(name, [])
        if not isinstance(values, list):
            raise ValueError(f"{path.name} 的 {name} 必須是清單")
        for value in values:
            if not isinstance(value, dict) or not str(value.get("reason", "")).strip():
                raise ValueError(f"{path.name} 的每筆 {name} 都必須填寫 reason")
        return values

    excluded_reviews = {
        (str(item["shop_key"]), str(item["review_id"])): str(item["reason"])
        for item in reasoned("exclude_reviews")
    }
    excluded_groups = {
        str(item["fingerprint"]): str(item["reason"])
        for item in reasoned("exclude_groups")
    }
    phrases = [
        normalize_text(str(item["text"]))
        for item in reasoned("common_phrases")
        if str(item.get("text", "")).strip()
    ]
    return AnalysisRules(excluded_reviews, excluded_groups, phrases)


def normalize_text(text: str) -> str:
    return KEEP_RE.sub("", unicodedata.normalize("NFKC", text).lower())


def lexical_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    left_grams = Counter(left[index : index + 2] for index in range(max(1, len(left) - 1)))
    right_grams = Counter(right[index : index + 2] for index in range(max(1, len(right) - 1)))
    overlap = sum((left_grams & right_grams).values())
    dice = (2 * overlap) / (sum(left_grams.values()) + sum(right_grams.values()))
    return max(sequence, dice)


def cosine(left: list[float], right: list[float]) -> float:
    # E5 output is normalized, so a dot product is sufficient.
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def run_analysis(
    settings: Settings,
    provider: EmbeddingProvider | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    db = Database(settings.database_path, settings.timezone)
    reviews = list(db.iter_reviews())
    model_name = settings.analysis_model
    settings_record = {
        "lexical_threshold": settings.analysis_lexical_threshold,
        "semantic_threshold": settings.analysis_semantic_threshold,
        "minimum_chinese_chars": 12,
        "short_exact_minimum": 3,
    }
    run_id = db.start_analysis(
        getattr(provider, "version", f"{model_name}:onnx"), settings_record, len(reviews)
    )

    def report(stage: str, percent: int, done: int) -> None:
        db.update_analysis_progress(run_id, stage, percent, done, len(reviews))
        if progress:
            progress(stage, done, len(reviews))

    try:
        rules = load_rules(settings.analysis_rules_path)
        items = _prepare(reviews, rules)
        report("載入本機語意模型", 10, 0)
        provider = provider or E5OnnxProvider(model_name)
        eligible = [item for item in items if item["eligible"]]
        vectors = provider.encode([item["normalized"] for item in eligible]) if eligible else []
        for item, vector in zip(eligible, vectors):
            item["embedding"] = vector
        report("比較評論相似度", 45, 0)
        groups: list[dict[str, Any]] = []
        pairs: list[dict[str, Any]] = []
        for scope in ("same", "cross"):
            scope_groups, scope_pairs = _analyze_scope(
                items,
                scope,
                settings.analysis_lexical_threshold,
                settings.analysis_semantic_threshold,
                rules,
            )
            groups.extend(scope_groups)
            pairs.extend(scope_pairs)
            report("建立同店群組" if scope == "same" else "建立跨店群組", 70 if scope == "same" else 90, len(items))
        result_reviews = _review_summaries(items, groups)
        report("寫入完整分析快照", 96, len(items))
        db.complete_analysis(run_id, result_reviews, groups, pairs)
        return {
            "run_id": run_id,
            "reviews": len(items),
            "groups": sum(1 for group in groups if not group.get("excluded_reason")),
            "suspected_reviews": sum(1 for item in result_reviews if item["label"] == "suspected"),
        }
    except Exception as exc:
        db.fail_analysis(run_id, str(exc))
        raise
    finally:
        db.close()


def _prepare(reviews: list[dict[str, Any]], rules: AnalysisRules) -> list[dict[str, Any]]:
    items = []
    exact_counts: Counter[str] = Counter()
    for review in reviews:
        normalized = normalize_text(str(review.get("text", "")))
        for phrase in rules.common_phrases:
            normalized = normalized.replace(phrase, "")
        exact_counts[normalized] += 1
        item = dict(review)
        item["normalized"] = normalized
        item["chinese_count"] = len(CHINESE_RE.findall(normalized))
        item["excluded"] = (item["shop_key"], item["review_id"]) in rules.excluded_reviews
        items.append(item)
    for item in items:
        item["eligible"] = (
            not item["excluded"]
            and bool(item["normalized"])
            and (
                item["chinese_count"] >= 12
                or exact_counts[item["normalized"]] >= 3
            )
        )
    return items


def _analyze_scope(
    items: list[dict[str, Any]],
    scope: str,
    lexical_threshold: float,
    semantic_threshold: float,
    rules: AnalysisRules,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    edges: dict[int, set[int]] = defaultdict(set)
    pair_values: dict[tuple[int, int], tuple[float, float]] = {}
    output_pairs = []
    short_counts = Counter(
        (
            item["shop_key"] if scope == "same" else "*",
            item["normalized"],
        )
        for item in items
        if item["eligible"]
    )
    for left_index, left in enumerate(items):
        if not left["eligible"]:
            continue
        for right_index in range(left_index + 1, len(items)):
            right = items[right_index]
            if not right["eligible"]:
                continue
            if scope == "same" and left["shop_key"] != right["shop_key"]:
                continue
            if scope == "cross" and left["shop_key"] == right["shop_key"]:
                continue
            lexical = lexical_similarity(left["normalized"], right["normalized"])
            semantic = cosine(left["embedding"], right["embedding"])
            exact_short = (
                left["normalized"] == right["normalized"]
                and (left["chinese_count"] < 12 or right["chinese_count"] < 12)
            )
            if (left["chinese_count"] < 12 or right["chinese_count"] < 12) and (
                not exact_short
                or short_counts[
                    (left["shop_key"] if scope == "same" else "*", left["normalized"])
                ] < 3
            ):
                continue
            if not exact_short and lexical < lexical_threshold and semantic < semantic_threshold:
                continue
            edges[left_index].add(right_index)
            edges[right_index].add(left_index)
            pair_values[(left_index, right_index)] = (lexical, semantic)
            output_pairs.append(
                {
                    "scope": scope,
                    "left_shop_key": left["shop_key"],
                    "left_review_id": left["review_id"],
                    "right_shop_key": right["shop_key"],
                    "right_review_id": right["review_id"],
                    "lexical": lexical,
                    "semantic": semantic,
                }
            )

    groups = []
    visited: set[int] = set()
    for start in edges:
        if start in visited:
            continue
        stack = [start]
        component: set[int] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            visited.add(current)
            stack.extend(edges[current] - component)
        if len(component) < 2:
            continue
        members = [items[index] for index in sorted(component)]
        fingerprint = sha256(
            "|".join(sorted(f"{item['shop_key']}:{item['review_id']}" for item in members)).encode()
        ).hexdigest()[:20]
        relevant_pairs = [
            (a, b, scores)
            for (a, b), scores in pair_values.items()
            if a in component and b in component
        ]
        evidence = _evidence(members, scope)
        label = "suspected" if evidence else "highly_similar"
        direction = _direction(members)
        group_id = f"{scope}-{fingerprint}"
        group_members = []
        for index in component:
            scores = [
                value for a, b, value in relevant_pairs if index in (a, b)
            ]
            group_members.append(
                {
                    "shop_key": items[index]["shop_key"],
                    "review_id": items[index]["review_id"],
                    "max_lexical": max((value[0] for value in scores), default=0),
                    "max_semantic": max((value[1] for value in scores), default=0),
                }
            )
        groups.append(
            {
                "group_id": group_id,
                "scope": scope,
                "fingerprint": fingerprint,
                "label": label,
                "direction": direction,
                "review_count": len(members),
                "shop_count": len({item["shop_key"] for item in members}),
                "max_lexical": max(value[0] for _, _, value in relevant_pairs),
                "max_semantic": max(value[1] for _, _, value in relevant_pairs),
                "latest_at": max(filter(None, (_posted_at(item) for item in members)), default=None),
                "evidence": evidence,
                "excluded_reason": rules.excluded_groups.get(fingerprint),
                "members": group_members,
            }
        )
    return groups, output_pairs


def _posted_at(item: dict[str, Any]) -> str | None:
    return item.get("estimated_posted_date") or item.get("back_calculated_at") or None


def _parse_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp()
    except ValueError:
        return None


def _evidence(members: list[dict[str, Any]], scope: str) -> list[str]:
    evidence = []
    dates = sorted(date for date in (_parse_date(_posted_at(item)) for item in members) if date)
    if len(dates) >= 3:
        for index in range(len(dates) - 2):
            if dates[index + 2] - dates[index] <= timedelta(days=14).total_seconds():
                evidence.append("14 天內至少 3 則相似評論")
                break
    profiles: dict[str, set[str]] = defaultdict(set)
    for item in members:
        url = str((item.get("profile") or {}).get("url", "")).strip()
        if url:
            profiles[url].add(item["shop_key"])
    if any(len(shops) >= 2 for shops in profiles.values()):
        evidence.append("同一 Google 個人檔案在至少 2 家店留下相似評論")
    if scope == "cross" and len(members) >= 3 and len({item["shop_key"] for item in members}) >= 2:
        evidence.append("至少 3 則相似內容跨越至少 2 家店")
    return evidence


def _direction(members: list[dict[str, Any]]) -> str:
    stars = [float(item["stars"]) for item in members if item.get("stars") is not None]
    if stars and all(star >= 4 for star in stars):
        return "positive"
    if stars and all(star <= 2 for star in stars):
        return "negative"
    return "mixed"


def _review_summaries(
    items: list[dict[str, Any]], groups: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    summaries = {
        (item["shop_key"], item["review_id"]): {
            "shop_key": item["shop_key"],
            "review_id": item["review_id"],
            "text_hash": sha256(item["text"].encode("utf-8")).hexdigest(),
            "embedding": _embedding_blob(item.get("embedding")),
            "label": "",
            "direction": "",
            "max_lexical": 0.0,
            "max_semantic": 0.0,
        }
        for item in items
    }
    for group in groups:
        if group.get("excluded_reason"):
            continue
        for member in group["members"]:
            item = summaries[(member["shop_key"], member["review_id"])]
            if group["label"] == "suspected" or not item["label"]:
                item["label"] = group["label"]
                item["direction"] = group["direction"]
            item["max_lexical"] = max(item["max_lexical"], member["max_lexical"])
            item["max_semantic"] = max(item["max_semantic"], member["max_semantic"])
    return list(summaries.values())


def _embedding_blob(vector: list[float] | None) -> bytes | None:
    if vector is None:
        return None
    try:
        import array

        return array.array("f", vector).tobytes()
    except (TypeError, ValueError):
        return json.dumps(vector, separators=(",", ":")).encode()

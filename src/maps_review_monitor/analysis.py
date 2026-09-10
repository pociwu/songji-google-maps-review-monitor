"""Local, restart-safe review similarity analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import combinations
import json
import logging
import os
from pathlib import Path
import re
from statistics import median
import unicodedata
from typing import Any, Callable, Protocol

import yaml

from .config import Settings
from .database import Database
from .evidence_inputs import (
    analysis_input_hash,
    canonical_contributor_id,
    verified_local_photo_entries,
    verified_local_photo_hashes,
)


LOG = logging.getLogger(__name__)
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
KEEP_RE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)
EVIDENCE_SCHEMA_VERSION = 2
HIGH_RISK_MINIMUM_PROFILES = 3
TIME_CLUSTER_WINDOW = timedelta(days=14)
PRECISE_TIME_UNITS = {"seconds", "minutes", "hours"}
BOUNDED_TIME_UNITS = PRECISE_TIME_UNITS | {"days"}


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


def _local_photo_hashes(
    item: dict[str, Any], media_root: Path | None, project_root: Path | None
) -> list[str]:
    return verified_local_photo_hashes(
        item, project_root=project_root, media_root=media_root
    )


def run_analysis(
    settings: Settings,
    provider: EmbeddingProvider | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    db = Database(settings.database_path, settings.timezone, settings.root)
    reviews = list(db.iter_reviews())
    model_name = settings.analysis_model
    settings_record = {
        "evidence_model_version": EVIDENCE_SCHEMA_VERSION,
        "grouping": "complete_linkage",
        "lexical_threshold": settings.analysis_lexical_threshold,
        "semantic_threshold": settings.analysis_semantic_threshold,
        "minimum_chinese_chars": 12,
        "short_exact_minimum": 3,
        "high_risk_minimum_profiles": HIGH_RISK_MINIMUM_PROFILES,
        "high_risk_minimum_independent_signal_families": 2,
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
        data_dir = Path(getattr(settings, "data_dir", settings.database_path.parent))
        project_root = Path(getattr(settings, "root", data_dir.parent))
        items = _prepare(reviews, rules, data_dir / "media", project_root)
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


def _prepare(
    reviews: list[dict[str, Any]],
    rules: AnalysisRules,
    media_root: Path | None = None,
    project_root: Path | None = None,
) -> list[dict[str, Any]]:
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
        profile_url = str((item.get("profile") or {}).get("url", "")).strip()
        item["contributor_id"] = canonical_contributor_id(profile_url)
        item["photo_hash_entries"] = verified_local_photo_entries(
            item, project_root=project_root, media_root=media_root
        )
        item["photo_hashes"] = sorted(
            {entry["hash"] for entry in item["photo_hash_entries"]}
        )
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
    for component in _complete_linkage_groups(items, scope, pair_values):
        members = [items[index] for index in sorted(component)]
        fingerprint = sha256(
            "|".join(sorted(f"{item['shop_key']}:{item['review_id']}" for item in members)).encode()
        ).hexdigest()[:20]
        relevant_pairs = [
            (a, b, scores)
            for (a, b), scores in pair_values.items()
            if a in component and b in component
        ]
        possible_pairs = _possible_pair_count(items, component, scope)
        evidence = _evidence_v2(
            members,
            scope,
            relevant_pairs,
            possible_pairs,
            lexical_threshold,
            semantic_threshold,
        )
        label = (
            "suspected"
            if evidence["level"] == "suspicious_high"
            else "highly_similar"
        )
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


def _complete_linkage_groups(
    items: list[dict[str, Any]],
    scope: str,
    pair_values: dict[tuple[int, int], tuple[float, float]],
) -> list[set[int]]:
    """Build deterministic cohesive groups; a bridge alone can never merge clusters."""
    nodes = sorted({index for pair in pair_values for index in pair})
    parent = {index: index for index in nodes}
    clusters = {index: {index} for index in nodes}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    ranked_edges = sorted(
        pair_values,
        key=lambda pair: (
            -max(pair_values[pair]),
            -min(pair_values[pair]),
            pair,
        ),
    )
    for left, right in ranked_edges:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            continue
        left_cluster, right_cluster = clusters[left_root], clusters[right_root]
        required_pairs = [
            tuple(sorted((a, b)))
            for a in left_cluster
            for b in right_cluster
            if scope == "same" or items[a]["shop_key"] != items[b]["shop_key"]
        ]
        if not required_pairs or not all(pair in pair_values for pair in required_pairs):
            continue
        keep, remove = sorted((left_root, right_root))
        parent[remove] = keep
        clusters[keep] = left_cluster | right_cluster
        clusters.pop(remove)

    return [cluster for _, cluster in sorted(clusters.items()) if len(cluster) >= 2]


def _possible_pair_count(
    items: list[dict[str, Any]], component: set[int], scope: str
) -> int:
    ordered = sorted(component)
    return sum(
        1
        for offset, left in enumerate(ordered)
        for right in ordered[offset + 1 :]
        if scope == "same" or items[left]["shop_key"] != items[right]["shop_key"]
    )


def _posted_at(item: dict[str, Any]) -> str | None:
    return item.get("back_calculated_at") or item.get("estimated_posted_date") or None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed
    except (TypeError, ValueError):
        return None


def _evidence_v2(
    members: list[dict[str, Any]],
    scope: str,
    relevant_pairs: list[tuple[int, int, tuple[float, float]]],
    possible_pairs: int,
    lexical_threshold: float,
    semantic_threshold: float,
) -> dict[str, Any]:
    lexical_values = [scores[0] for _, _, scores in relevant_pairs]
    semantic_values = [scores[1] for _, _, scores in relevant_pairs]
    contributor_ids = {item.get("contributor_id", "") for item in members} - {""}
    profile_known_count = sum(bool(item.get("contributor_id")) for item in members)
    date_precision = Counter(_date_precision(item) for item in members)
    date_known_count = len(members) - date_precision["unknown"]
    direct_pairs = len(relevant_pairs)
    cohesion_points, minimum_threshold_margin = _cohesion_score(
        relevant_pairs, lexical_threshold, semantic_threshold
    )
    metrics = {
        "min_lexical": _rounded(min(lexical_values, default=0.0)),
        "median_lexical": _rounded(float(median(lexical_values)) if lexical_values else 0.0),
        "max_lexical": _rounded(max(lexical_values, default=0.0)),
        "min_semantic": _rounded(min(semantic_values, default=0.0)),
        "median_semantic": _rounded(float(median(semantic_values)) if semantic_values else 0.0),
        "max_semantic": _rounded(max(semantic_values, default=0.0)),
        "pair_density": _rounded(direct_pairs / possible_pairs if possible_pairs else 0.0),
        "minimum_threshold_margin": _rounded(minimum_threshold_margin),
        "direct_pairs": direct_pairs,
        "possible_pairs": possible_pairs,
        "distinct_profiles": len(contributor_ids),
        "shop_count": len({item["shop_key"] for item in members}),
        "review_count": len(members),
        "date_known_count": date_known_count,
        "date_total": len(members),
        "date_precise_count": date_precision["precise"],
        "date_bounded_count": date_precision["bounded"],
        "date_coarse_count": date_precision["coarse"],
    }
    coverage = {
        "profile_known_count": profile_known_count,
        "profile_total": len(members),
        "distinct_profiles": len(contributor_ids),
        "date_known_count": date_known_count,
        "date_precise_count": date_precision["precise"],
        "date_total": len(members),
        "profiles": _coverage(profile_known_count, len(members)),
        "dates": _coverage(date_known_count, len(members)),
        "bounded_dates": _coverage(
            date_precision["precise"] + date_precision["bounded"], len(members)
        ),
        "photos": {
            "reviews_with_hashed_photo": sum(bool(item.get("photo_hashes")) for item in members),
            "hashed_files": sum(len(item.get("photo_hashes") or []) for item in members),
            "total": len(members),
        },
    }

    signals = [
        _temporal_signal(members),
        _reviewer_cohort_signal(members, scope),
        _shared_photo_signal(members),
    ]
    score_breakdown = [
        {
            "key": "content_candidate",
            "title": "內容相似候選",
            "points": 20,
            "max_points": 20,
        },
        {
            "key": "group_cohesion",
            "title": "群組相似門檻餘裕",
            "points": cohesion_points,
            "max_points": 10,
        },
        *[
            {
                "key": signal["key"],
                "title": signal["title"],
                "points": signal["points"],
                "max_points": signal["max_points"],
            }
            for signal in signals
        ],
    ]
    evidence_score = sum(item["points"] for item in score_breakdown)
    matched_families = sorted(
        {
            signal["family"]
            for signal in signals
            if signal["matched"] and signal["independent_for_level"]
        }
    )
    supporting_context = sorted(
        {
            signal["family"]
            for signal in signals
            if signal["matched"] and not signal["independent_for_level"]
        }
    )
    high_eligible = (
        len(matched_families) >= 2
        and len(contributor_ids) >= HIGH_RISK_MINIMUM_PROFILES
    )
    if high_eligible:
        level = "suspicious_high"
        summary = f"命中 {len(matched_families)} 類獨立非內容訊號，且可確認 {len(contributor_ids)} 個 Google 個人檔案；列為高優先人工複核。"
    elif matched_families:
        level = "review"
        summary = f"命中 {len(matched_families)} 類獨立非內容訊號，但尚未達高優先複核條件；需要人工複核。"
    else:
        level = "content_similar"
        summary = "目前只有內容相似，沒有足夠的獨立行為證據。"
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "level": level,
        "score": evidence_score,
        "score_label": "證據排序分數",
        "score_breakdown": score_breakdown,
        "summary": summary,
        "signals": signals,
        "metrics": metrics,
        "coverage": coverage,
        "decision": {
            "independent_signal_families": matched_families,
            "independent_signal_count": len(matched_families),
            "supporting_context_families": supporting_context,
            "minimum_profiles_for_high": HIGH_RISK_MINIMUM_PROFILES,
            "minimum_signal_families_for_high": 2,
            "high_eligible": high_eligible,
            "content_similarity_counts_as_independent_evidence": False,
            "cross_shop_group_size_counts_as_independent_evidence": False,
            "star_direction_counts_as_independent_evidence": False,
            "score_not_probability": True,
            "level_derived_from_score": False,
        },
    }


def _cohesion_score(
    pairs: list[tuple[int, int, tuple[float, float]]],
    lexical_threshold: float,
    semantic_threshold: float,
) -> tuple[int, float]:
    """Score how far the weakest direct pair clears either candidate threshold."""

    def margin(score: float, threshold: float) -> float:
        if threshold >= 1:
            return 1.0 if score >= 1 else 0.0
        return max(0.0, min(1.0, (score - threshold) / (1 - threshold)))

    pair_margins = [
        max(margin(scores[0], lexical_threshold), margin(scores[1], semantic_threshold))
        for _, _, scores in pairs
    ]
    weakest = min(pair_margins, default=0.0)
    return round(weakest * 10), weakest


def _rounded(value: float) -> float:
    return round(float(value), 4)


def _coverage(known: int, total: int) -> dict[str, int]:
    return {
        "known": known,
        "total": total,
        "percent": round(known / total * 100) if total else 0,
    }


def _date_precision(item: dict[str, Any]) -> str:
    unit = str(item.get("time_relative_unit") or "")
    if _parse_datetime(str(item.get("back_calculated_at") or "")):
        if unit in PRECISE_TIME_UNITS:
            return "precise"
        if unit == "days":
            return "bounded"
        return "coarse"
    estimated = str(item.get("estimated_posted_date") or "")
    if estimated:
        try:
            datetime.fromisoformat(estimated)
            return "coarse"
        except ValueError:
            pass
    return "unknown"


def _temporal_interval(
    item: dict[str, Any],
) -> tuple[datetime, datetime, datetime, str] | None:
    unit = str(item.get("time_relative_unit") or "")
    if unit not in BOUNDED_TIME_UNITS:
        return None
    center = _parse_datetime(str(item.get("back_calculated_at") or ""))
    if center is None:
        return None
    uncertainty = {
        "seconds": timedelta(seconds=1),
        "minutes": timedelta(minutes=1),
        "hours": timedelta(hours=1),
        "days": timedelta(days=1),
    }[unit]
    return center - uncertainty, center + uncertainty, center, unit


def _temporal_signal(members: list[dict[str, Any]]) -> dict[str, Any]:
    intervals = [
        (*interval, str(item.get("contributor_id") or ""))
        for item in members
        if (interval := _temporal_interval(item)) is not None
    ]
    intervals.sort(key=lambda value: value[2])
    best: list[tuple[datetime, datetime, datetime, str, str]] = []
    best_span: timedelta | None = None
    for left in range(len(intervals)):
        earliest = intervals[left][0]
        latest = intervals[left][1]
        for right in range(left, len(intervals)):
            earliest = min(earliest, intervals[right][0])
            latest = max(latest, intervals[right][1])
            if latest - earliest > TIME_CLUSTER_WINDOW:
                break
            window = intervals[left : right + 1]
            profiles = {value[4] for value in window} - {""}
            candidate_span = latest - earliest
            if len(profiles) >= HIGH_RISK_MINIMUM_PROFILES and (
                len(window) > len(best)
                or (
                    len(window) == len(best)
                    and (best_span is None or candidate_span < best_span)
                )
            ):
                best = window
                best_span = candidate_span
    matched = len(best) >= 3
    span_hours = round(best_span.total_seconds() / 3600, 1) if best_span else None
    bounded_profiles = {value[4] for value in intervals} - {""}
    if matched and span_hours is not None:
        status = "matched"
        detail = (
            f"{len(best)} 則評論由 {len({value[4] for value in best} - {''})} 個可確認個人檔案發布，"
            f"在納入相對時間誤差後仍鄰近於 {span_hours:g} 小時內；"
            "尚未相對店家平時流量校正，只作背景提示，不作等級升級證據。"
        )
    elif len(bounded_profiles) < HIGH_RISK_MINIMUM_PROFILES:
        status = "insufficient"
        detail = (
            f"資料不足：只有 {len(intervals)} 則可界定誤差的時間資料，"
            f"涉及 {len(bounded_profiles)} 個可確認個人檔案；至少需要 {HIGH_RISK_MINIMUM_PROFILES} 個。"
        )
    else:
        status = "not_matched"
        detail = (
            f"已檢查 {len(intervals)} 則可界定誤差的時間資料，"
            f"未發現至少 3 個可確認個人檔案能完整落在 {TIME_CLUSTER_WINDOW.days} 天鄰近視窗內。"
        )
    return {
        "key": "time_proximity",
        "family": "temporal_context",
        "title": "時間鄰近（未校正店家基準）",
        "matched": matched,
        "status": status,
        "independent_for_level": False,
        "points": 10 if matched else 0,
        "max_points": 25,
        "detail": detail,
        "observed": {
            "bounded_review_count": len(intervals),
            "cluster_review_count": len(best),
            "cluster_profile_count": len({value[4] for value in best} - {""}),
            "guaranteed_span_hours": span_hours,
            "maximum_window_days": TIME_CLUSTER_WINDOW.days,
        },
    }


def _reviewer_cohort_signal(
    members: list[dict[str, Any]], scope: str
) -> dict[str, Any]:
    profile_shops: dict[str, set[str]] = defaultdict(set)
    for item in members:
        contributor_id = str(item.get("contributor_id") or "")
        if contributor_id:
            profile_shops[contributor_id].add(str(item["shop_key"]))
    repeated = {
        profile: shops for profile, shops in profile_shops.items() if len(shops) >= 2
    }
    pair_profiles: dict[tuple[str, str], set[str]] = defaultdict(set)
    for profile, shops in repeated.items():
        for shop_pair in combinations(sorted(shops), 2):
            pair_profiles[shop_pair].add(profile)
    cohort_shops, cohort_profiles = (
        max(
            pair_profiles.items(),
            key=lambda value: (len(value[1]), value[0]),
        )
        if pair_profiles
        else ((), set())
    )
    cohort_size = len(cohort_profiles)
    matched = scope == "cross" and cohort_size >= 2 and len(cohort_shops) >= 2
    if matched:
        status = "matched"
        detail = f"{cohort_size} 個可確認 Google 個人檔案共同在同一組 {len(cohort_shops)} 家店出現相似評論。"
    elif scope != "cross":
        status = "not_applicable"
        detail = "此訊號只適用於跨店候選群組。"
    elif len(profile_shops) < 2:
        status = "insufficient"
        detail = f"資料不足：只有 {len(profile_shops)} 個可解析的 Google contributor ID。"
    else:
        status = "not_matched"
        detail = "已檢查可確認個人檔案，未發現至少 2 個評論者共同跨越至少 2 家相同店家。"
    return {
        "key": "reviewer_cohort",
        "family": "reviewer_network",
        "title": "跨店評論者群組",
        "matched": matched,
        "status": status,
        "independent_for_level": True,
        "points": 25 if matched else 0,
        "max_points": 25,
        "detail": detail,
        "observed": {
            "canonical_profile_count": len(profile_shops),
            "repeated_profile_count": len(repeated),
            "largest_cohort_profile_count": cohort_size,
            "largest_cohort_shop_count": len(cohort_shops),
        },
    }


def _shared_photo_signal(members: list[dict[str, Any]]) -> dict[str, Any]:
    photo_profiles: dict[str, set[str]] = defaultdict(set)
    photo_members: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for item in members:
        contributor_id = str(item.get("contributor_id") or "")
        if not contributor_id:
            continue
        entries = item.get("photo_hash_entries")
        if not isinstance(entries, list):
            entries = [
                {"hash": photo_hash, "path": ""}
                for photo_hash in item.get("photo_hashes") or []
            ]
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("hash"):
                continue
            key = str(entry["hash"])
            photo_profiles[key].add(contributor_id)
            member_key = (str(item.get("shop_key", "")), str(item.get("review_id", "")))
            member = photo_members[key].setdefault(
                member_key,
                {
                    "shop_key": member_key[0],
                    "shop_name": str(item.get("shop_name", "")),
                    "review_id": member_key[1],
                    "author": str(item.get("author", "")),
                    "contributor_id": contributor_id,
                    "photo_paths": [],
                },
            )
            stored_path = str(entry.get("path") or "")
            if stored_path and stored_path not in member["photo_paths"]:
                member["photo_paths"].append(stored_path)
    shared = {key: profiles for key, profiles in photo_profiles.items() if len(profiles) >= 2}
    largest = max((len(profiles) for profiles in shared.values()), default=0)
    matched = bool(shared)
    hashed_file_count = len(photo_profiles)
    if matched:
        status = "matched"
        detail = f"發現 {len(shared)} 張內容完全相同的本機照片，由最多 {largest} 個不同個人檔案使用。"
    elif not hashed_file_count:
        status = "insufficient"
        detail = "資料不足：群組中沒有可讀取且位於媒體白名單的本機評論照片。"
    else:
        status = "not_matched"
        detail = f"已檢查 {hashed_file_count} 張不同的本機照片，未發現不同可確認個人檔案共用完全相同的檔案。"
    return {
        "key": "shared_photo",
        "family": "media",
        "title": "跨帳號共用照片",
        "matched": matched,
        "status": status,
        "independent_for_level": True,
        "points": 20 if matched else 0,
        "max_points": 20,
        "detail": detail,
        "observed": {
            "hashed_photo_count": hashed_file_count,
            "shared_photo_count": len(shared),
            "largest_distinct_profile_count": largest,
            "shared_matches": [
                {
                    "fingerprint": photo_hash[:12],
                    "distinct_profile_count": len(shared[photo_hash]),
                    "members": list(photo_members[photo_hash].values()),
                }
                for photo_hash in sorted(shared)[:20]
            ],
        },
    }


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
            # Keep the historical column name for a migration-free deployment;
            # v2 stores a versioned fingerprint of every evidence input.
            "text_hash": analysis_input_hash(item),
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

"""Deterministic and auditable decomposition of a game idea."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import GameRequirement


@dataclass(frozen=True, slots=True)
class _Capability:
    key: str
    title: str
    query: str
    keywords: tuple[str, ...]
    rationale: str


_CAPABILITIES: tuple[_Capability, ...] = (
    _Capability(
        "networking", "マルチプレイ通信",
        "unity multiplayer networking netcode",
        ("multiplayer", "co-op", "coop", "online", "マルチ", "協力", "対戦", "オンライン"),
        "複数プレイヤーの状態同期と通信方式が必要です。",
    ),
    _Capability(
        "lobby_matchmaking", "ロビー・マッチメイク",
        "unity lobby matchmaking relay multiplayer",
        ("lobby", "matchmaking", "room", "ロビー", "マッチング", "ルーム"),
        "参加・退出・部屋検索をゲーム本体から分離します。",
    ),
    _Capability(
        "character_controller", "キャラクター操作",
        "unity character controller movement package",
        ("action", "platformer", "fps", "tps", "arpg", "移動", "操作", "アクション"),
        "移動、接地、坂、段差などの基礎挙動が必要です。",
    ),
    _Capability(
        "camera", "カメラ",
        "unity camera system cinemachine",
        ("camera", "third person", "first person", "top down", "isometric", "カメラ", "三人称", "一人称", "見下ろし"),
        "ゲーム形式に合う追従・構図・遷移を用意します。",
    ),
    _Capability(
        "input", "入力",
        "unity input system controller rebinding",
        ("controller", "gamepad", "keyboard", "touch", "steam deck", "ゲームパッド", "コントローラー", "キーボード", "タッチ"),
        "対象デバイスの入力とリバインドを統一します。",
    ),
    _Capability(
        "save_system", "セーブ",
        "unity save system serialization",
        ("save", "persistence", "progression", "セーブ", "保存", "進行", "育成"),
        "進行状態と設定を安全に永続化します。",
    ),
    _Capability(
        "inventory", "インベントリ",
        "unity inventory item system",
        ("inventory", "item", "loot", "equipment", "インベントリ", "アイテム", "装備", "収集"),
        "アイテム定義、所持、装備、保存を一貫させます。",
    ),
    _Capability(
        "combat", "戦闘",
        "unity combat ability damage system",
        ("combat", "battle", "shooter", "melee", "戦闘", "バトル", "攻撃", "シューティング", "ボス"),
        "ダメージ、体力、ターゲット、状態変化を整理します。",
    ),
    _Capability(
        "enemy_ai", "敵AI・ナビゲーション",
        "unity enemy ai behavior tree navigation",
        ("enemy", "npc", "ai", "stealth", "敵", "エネミー", "ステルス"),
        "移動経路と意思決定を分けて実装します。",
    ),
    _Capability(
        "interaction", "探索・インタラクション",
        "unity interaction examine pickup lock framework",
        (
            "interaction", "interact", "examine", "pickup", "door", "key",
            "探索", "調べる", "拾う", "扉", "ドア", "鍵", "かぎ",
        ),
        "扉、鍵、調査対象、取得物を共通の操作規約で扱います。",
    ),
    _Capability(
        "puzzle", "パズル・脱出ギミック",
        "unity escape puzzle logic clue keypad lock",
        (
            "puzzle", "escape room", "escape game", "riddle", "clue",
            "keypad", "パズル", "脱出", "だしゅつ", "謎解き", "ギミック",
        ),
        "手掛かり、条件判定、解除状態と再試行をデータとして管理します。",
    ),
    _Capability(
        "horror_atmosphere", "ホラー演出・環境",
        "unity horror scary abandoned dark environment props",
        (
            "horror", "scary", "haunted", "ホラー", "恐怖", "怖い",
            "廃墟", "心霊",
        ),
        "舞台美術、視界制限、驚かせ方をゲーム進行と分離して設計します。",
    ),
    _Capability(
        "lighting", "照明・ポストプロセス",
        "unity lighting volumetric flashlight post processing darkness",
        (
            "lighting", "light", "volumetric", "flashlight", "darkness",
            "照明", "ライト", "懐中電灯", "暗闇", "ポストプロセス",
        ),
        "可読性を保ちながら暗さ、霧、ライト、画面効果を統一します。",
    ),
    _Capability(
        "procedural_generation", "プロシージャル生成",
        "unity procedural dungeon level generation",
        ("procedural", "random dungeon", "roguelike", "roguelite", "自動生成", "ランダム生成", "ローグライク", "ローグライト"),
        "再現可能なシードとレベル生成パイプラインが必要です。",
    ),
    _Capability(
        "dialogue_quest", "会話・クエスト",
        "unity dialogue quest narrative system",
        ("dialogue", "quest system", "quests", "mission", "narrative", "visual novel", "会話", "クエスト", "ノベル", "物語"),
        "分岐、条件、文章進行を管理します。",
    ),
    _Capability(
        "localization", "ローカライズ",
        "unity localization package",
        ("localization", "multilingual", "translation", "多言語", "翻訳", "ローカライズ", "日本語", "英語"),
        "文字列・フォント・画像差し替えを管理します。",
    ),
    _Capability(
        "ui", "UI基盤",
        "unity ui toolkit menu hud",
        ("ui", "hud", "menu", "メニュー", "画面", "インターフェース"),
        "メニュー、HUD、設定画面の方針を揃えます。",
    ),
    _Capability(
        "audio", "オーディオ",
        "unity audio manager adaptive music",
        ("audio", "music", "sound", "voice", "音", "bgm", "効果音", "ボイス"),
        "BGM、効果音、ミキサー、音量設定を統合します。",
    ),
    _Capability(
        "xr", "XR・VRインタラクション",
        "unity xr interaction toolkit vr",
        ("vr", "xr", "openxr", "quest", "virtual reality", "仮想現実", "掴む", "つかむ"),
        "XRランタイム、入力、掴み、移動方式を統一します。",
    ),
    _Capability(
        "mobile", "モバイル対応",
        "unity mobile optimization touch controls",
        ("mobile", "android", "ios", "スマホ", "モバイル", "タブレット"),
        "タッチ入力、画面比率、発熱・メモリ制約を考慮します。",
    ),
    _Capability(
        "steam", "Steam連携",
        "unity steamworks achievements workshop",
        ("steam", "steam deck", "achievement", "実績", "ワークショップ"),
        "実績、オーバーレイ、配布プラットフォーム連携を管理します。",
    ),
    _Capability(
        "addressables", "アセット配信・ロード",
        "unity addressables asset management",
        ("dlc", "downloadable", "live service", "addressable", "追加コンテンツ", "ライブサービス"),
        "非同期ロード、依存関係、追加配信を管理します。",
    ),
    _Capability(
        "visual_assets", "ビジュアル・環境アセット",
        "unity 2d 3d environment art models sprites city nature dungeon",
        (
            "environment", "environment art", "2d art", "3d art", "sprite",
            "city", "urban", "nature", "dungeon", "背景", "環境", "街", "都市",
            "建物", "自然", "ダンジョン", "2d", "3d",
        ),
        "舞台、背景、プロップ、スプライトなど制作量の大きい素材を整理します。",
    ),
    _Capability(
        "character_art", "キャラクター・クリーチャー",
        "unity character creature model avatar art",
        (
            "character art", "character model", "creature", "monster", "avatar",
            "キャラクター", "クリーチャー", "モンスター", "アバター",
        ),
        "キャラクター、敵、衣装、クリーチャーの素材要件を整理します。",
    ),
    _Capability(
        "animation", "アニメーション",
        "unity character animation mocap controller",
        (
            "animation", "mocap", "motion", "rig", "アニメーション", "モーション",
            "モーションキャプチャ", "リグ",
        ),
        "移動、戦闘、演出に必要なモーションと制御方法を揃えます。",
    ),
    _Capability(
        "vfx", "VFX・パーティクル",
        "unity vfx particle effects",
        (
            "vfx", "particle", "effects", "effect", "エフェクト", "パーティクル",
            "爆発", "魔法演出",
        ),
        "戦闘、環境、フィードバック用の視覚効果を整理します。",
    ),
    _Capability(
        "shaders_materials", "シェーダー・マテリアル",
        "unity shader material texture rendering",
        (
            "shader", "material", "texture", "シェーダー", "マテリアル", "テクスチャ",
            "トゥーン", "水面",
        ),
        "描画表現、材質、テクスチャとRender Pipeline互換性を確認します。",
    ),
    _Capability(
        "editor_tools", "制作・Editorツール",
        "unity editor productivity level design tools",
        (
            "editor tool", "level design tool", "workflow", "エディタ拡張", "制作ツール",
            "レベルデザイン", "ワークフロー",
        ),
        "反復作業やレベル制作を支援するツールを比較します。",
    ),
)

_BASELINE_KEYS = ("input", "ui", "save_system", "visual_assets")
_ARCHETYPE_EXPANSIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("roguelike", "roguelite", "ローグライク", "ローグライト"),
        ("combat", "inventory", "procedural_generation", "enemy_ai"),
    ),
    (
        ("multiplayer", "coop", "co-op", "マルチ", "協力", "オンライン"),
        ("networking", "lobby_matchmaking"),
    ),
    (
        ("visual novel", "ノベル", "会話中心"),
        ("dialogue_quest", "localization"),
    ),
    (
        ("fps", "tps", "third person", "first person", "三人称", "一人称"),
        ("character_controller", "camera", "combat"),
    ),
    (
        ("escape room", "escape game", "脱出", "だしゅつ", "謎解き"),
        ("character_controller", "camera", "interaction", "puzzle", "audio"),
    ),
    (
        ("horror", "scary", "haunted", "ホラー", "恐怖", "怖い"),
        (
            "character_controller", "camera", "interaction",
            "horror_atmosphere", "lighting", "audio",
        ),
    ),
)


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _contains(text: str, keyword: str) -> bool:
    normalized_keyword = _normalized(keyword)
    if re.fullmatch(r"[a-z0-9 _-]+", normalized_keyword):
        return bool(re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])",
            text,
        ))
    return normalized_keyword in text


def derive_requirements(
    prompt: str,
    *,
    platform: str = "pc",
    maximum: int = 14,
) -> tuple[GameRequirement, ...]:
    """Return a bounded, explainable requirement list."""

    text = _normalized(prompt)
    selected: dict[str, str] = {}
    for capability in _CAPABILITIES:
        if any(_contains(text, keyword) for keyword in capability.keywords):
            selected[capability.key] = "high"
    for triggers, expansions in _ARCHETYPE_EXPANSIONS:
        if any(_contains(text, trigger) for trigger in triggers):
            for key in expansions:
                selected.setdefault(key, "high")
    for key in _BASELINE_KEYS:
        selected.setdefault(key, "normal")
    if platform in {"mobile", "android", "ios"}:
        selected["mobile"] = "high"
        selected["input"] = "high"
    if platform in {"vr", "quest", "xr"}:
        selected["xr"] = "high"
        selected["input"] = "high"
    if "networking" in selected:
        selected.setdefault("lobby_matchmaking", "normal")
    if "character_controller" in selected:
        selected.setdefault("camera", "normal")

    definitions = {item.key: item for item in _CAPABILITIES}
    index = {item.key: position for position, item in enumerate(_CAPABILITIES)}
    ordered = sorted(
        selected.items(),
        key=lambda item: (0 if item[1] == "high" else 1, index[item[0]]),
    )[:max(1, min(int(maximum), 20))]
    return tuple(
        GameRequirement(
            key=key,
            title=definitions[key].title,
            priority=priority,
            query=definitions[key].query,
            rationale=definitions[key].rationale,
        )
        for key, priority in ordered
    )


def requirement_categories() -> tuple[dict[str, str], ...]:
    """Return the stable feature taxonomy used by manual catalog entries."""

    return tuple(
        {"key": capability.key, "title": capability.title}
        for capability in _CAPABILITIES
    )


__all__ = ["derive_requirements", "requirement_categories"]

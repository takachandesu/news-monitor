"""
日本株ADR乖離率ランキングを毎朝7:30 JSTにXに投稿するスクリプト。
adr-data.json を取得 → ベスト4・ワースト4を1ツイートで投稿。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import requests
import tweepy


JST = timezone(timedelta(hours=9))
JSON_URL = "https://moo-stock-blog.com/adr-data.json"

# 長い銘柄名の短縮表記
NAME_SHORT = {
    "三菱UFJフィナンシャル・グループ": "三菱UFJ",
    "三井住友フィナンシャルグループ": "三井住友FG",
    "三井住友トラストグループ": "三井住友信託",
    "みずほフィナンシャルグループ": "みずほFG",
    "ソフトバンクグループ": "SBG",
    "ファーストリテイリング": "ファストリ",
    "東京エレクトロン": "東エレク",
    "ニトリホールディングス": "ニトリ",
    "セブン&アイ・ホールディングス": "セブン&i",
    "アステラス製薬": "アステラス",
    "オリエンタルランド": "OLC",
    "ANAホールディングス": "ANA",
    "Japan Post Holdings": "日本郵政",
    "MS&ADインシュアランス": "MS&AD",
    "SOMPOホールディングス": "SOMPO",
    "東京海上HD": "東京海上",
    "サントリー食品インターナショナル": "サントリーBF",
    "アサヒグループHD": "アサヒ",
    "キリンホールディングス": "キリン",
    "東京電力HD": "東電HD",
    "関西電力": "関電",
    "大和ハウス工業": "大和ハウス",
    "積水ハウス": "積水ハウス",
    "ダイキン工業": "ダイキン",
    "JR東日本": "JR東",
    "JR東海": "JR東海",
    "JR西日本": "JR西",
    "日本郵船": "郵船",
    "商船三井": "商船三井",
    "日本製鉄": "日鉄",
    "三菱重工業": "三菱重",
    "川崎重工業": "川崎重",
    "信越化学工業": "信越化学",
    "住友電気工業": "住友電工",
    "住友金属鉱山": "住友金属鉱",
    "三井金属鉱業": "三井金属",
    "中外製薬": "中外製薬",
    "リクルートHD": "リクルート",
    "ENEOSホールディングス": "ENEOS",
    "アドバンテスト": "アドバンテ",
    "パナソニックHD": "パナソニック",
    "三菱電機": "三菱電機",
    "ブリヂストン": "ブリヂストン",
    "Pan Pacific International": "パンパシ",
    "パン・パシフィックHD": "パンパシHD",
}


def fetch_data():
    """ロリポップから adr-data.json を取得（WAF回避のためUser-Agent指定）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language":

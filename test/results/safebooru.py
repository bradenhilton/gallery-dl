# -*- coding: utf-8 -*-

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

from gallery_dl.extractor import gelbooru_v02


__tests__ = (
{
    "#url"     : "https://safebooru.org/index.php?page=post&s=list&tags=bonocho",
    "#category": ("gelbooru_v02", "safebooru", "tag"),
    "#class"   : gelbooru_v02.GelbooruV02TagExtractor,
    "#sha1_url"    : "17c61b386530cf4c30842c9f580d15ef1cd09586",
    "#sha1_content": "e5ad4c5bf241b1def154958535bef6c2f6b733eb",
    "#results" : (
        "https://safebooru.org/images/344/3894735145db4f94cc6a3839004ebf4f16a9bc10.jpg",
        "https://safebooru.org/images/344/ae7198cbc41bed9b282fe5ec00b5b91509c53b30.jpg",
        "https://safebooru.org/images/264/458a42a01ca7090aca27a85c814557bee9e22a63.jpg",
        "https://safebooru.org/images/263/00a74a8736a7691dc1df3def867c32d5ad0fadeb.jpg",
    ),

    "search_tags" : "bonocho",
    "search_count": 4,
},

{
    "#url"     : "https://safebooru.org/index.php?page=post&s=list&tags=all",
    "#category": ("gelbooru_v02", "safebooru", "tag"),
    "#class"   : gelbooru_v02.GelbooruV02TagExtractor,
    "#range"   : "1-3",
    "#count"   : 3,

    "total": range(5_600_000, 6_000_000),
},

{
    "#url"     : "https://safebooru.org/index.php?page=post&s=list&tags=",
    "#category": ("gelbooru_v02", "safebooru", "tag"),
    "#class"   : gelbooru_v02.GelbooruV02TagExtractor,
},

{
    "#url"     : "https://safebooru.org/index.php?page=pool&s=show&id=11",
    "#category": ("gelbooru_v02", "safebooru", "pool"),
    "#class"   : gelbooru_v02.GelbooruV02PoolExtractor,
    "#count"   : 5,
},

{
    "#url"     : "https://safebooru.org/index.php?page=favorites&s=view&id=17567",
    "#category": ("gelbooru_v02", "safebooru", "favorite"),
    "#class"   : gelbooru_v02.GelbooruV02FavoriteExtractor,
    "#count"   : 2,
},

{
    "#url"     : "https://safebooru.org/index.php?page=post&s=view&id=1169132",
    "#category": ("gelbooru_v02", "safebooru", "post"),
    "#class"   : gelbooru_v02.GelbooruV02PostExtractor,
    "#options"     : {"tags": True},
    "#sha1_url"    : "cf05e37a3c62b2d55788e2080b8eabedb00f999b",
    "#sha1_content": "93b293b27dabd198afafabbaf87c49863ac82f27",

    "tags_artist"   : "kawanakajima",
    "tags_character": "heath_ledger ronald_mcdonald the_joker",
    "tags_copyright": "dc_comics mcdonald's the_dark_knight",
    "tags_metadata" : "tagme",
},

)

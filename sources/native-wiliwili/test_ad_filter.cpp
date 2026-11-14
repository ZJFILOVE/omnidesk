#include "bilibili/result/home_result.h"
#include "bilibili/result/home_hots_all_result.h"
#include "bilibili/result/home_hots_rank.h"
#include "bilibili/result/home_hots_weekly_result.h"
#include "bilibili/result/home_hots_history_result.h"
#include "bilibili/result/search_result.h"
#include "bilibili/result/dynamic_video.h"
#include "bilibili/result/dynamic_article.h"
#include "bilibili/result/video_detail_result.h"
#include "bilibili/result/home_live_result.h"
#include <cassert>
#include <iostream>

using nlohmann::json;

int main() {
    // 1. Recommend feed: business_info / ad_info / cm_info / goto!=av / rcmd_reason / normal video
    {
        json rcmd = {
            {"item", {
                {{"goto", "av"}, {"id", 101}, {"bvid", "BV1real00001"}, {"cid", 1}, {"title", "正常推荐视频"},
                 {"pic", "https://example.invalid/1.jpg"}, {"duration", 120}, {"pubdate", 1700000000},
                 {"owner", {{"mid", 10}, {"name", "UP主"}}}, {"stat", {{"view", 1000}, {"danmaku", 10}}},
                 {"business_info", nullptr}, {"ad_info", nullptr},
                 {"rcmd_reason", {{"content", "10万点赞"}, {"reason_type", 3}}}},
                {{"goto", "av"}, {"id", 102}, {"bvid", "BV1ad0000002"}, {"cid", 2}, {"title", "推广视频卡片"},
                 {"business_info", {{"is_ad", true}, {"adver_name", "某品牌"}, {"url", "https://ad.invalid"},
                                    {"business_mark", {{"text", "广告"}}},
                                    {"archive", {{"aid", 102}, {"bvid", "BV1ad0000002"}, {"cid", 2}, {"title", "推广"}, {"pic", ""}, {"duration", 30}, {"pubdate", 0}, {"owner", {{"mid", 1}, {"name", "ad"}}}, {"stat", {{"view", 1}, {"danmaku", 0}}}}}}}},
                {{"goto", "ad"}, {"id", 0}, {"bvid", ""}, {"title", "网页贴片广告"},
                 {"business_info", {{"is_ad", false}, {"url", "https://ad.invalid"}, {"adver_name", "品牌"}}}},
                {{"goto", "av"}, {"id", 103}, {"bvid", "BV1ad0000003"}, {"cid", 3}, {"title", "ad_info 贴片"},
                 {"ad_info", {{"creative_id", 888}}}},
                {{"goto", "av"}, {"id", 104}, {"bvid", "BV1ad0000004"}, {"cid", 4}, {"title", "创作推广"},
                 {"rcmd_reason", {{"content", "创作推广"}, {"reason_type", 0}}}},
                {{"goto", "live"}, {"id", 105}, {"bvid", "BV1live00005"}, {"title", "直播推广"}}
            }}
        };
        auto parsed = rcmd.get<bilibili::RecommendVideoListResultWrapper>();
        assert(parsed.item.size() == 1);
        assert(parsed.item[0].bvid == "BV1real00001");
        assert(!parsed.item[0].isAd());
    }

    // 2. Popular / Rank / Weekly / History feeds
    {
        json hots = {
            {"no_more", false},
            {"list", {
                {{"aid", 1}, {"bvid", "BV1pop000001"}, {"cid", 1}, {"pic", ""}, {"title", "热门正片"},
                 {"duration", 60}, {"pubdate", 0}, {"owner", {{"mid", 1}, {"name", "up"}}}, {"stat", {{"view", 1}, {"danmaku", 0}}}},
                {{"aid", 2}, {"bvid", "BV1popAd0002"}, {"cid", 2}, {"pic", ""}, {"title", "热门广告"},
                 {"ad_info", {{"creative_id", 1}}}, {"owner", {{"mid", 2}, {"name", "ad"}}}, {"stat", {{"view", 1}, {"danmaku", 0}}}}
            }}
        };
        auto parsed = hots.get<bilibili::HotsAllVideoListResultWrapper>();
        assert(parsed.list.size() == 1);
        assert(parsed.list[0].bvid == "BV1pop000001");

        json weekly = {
            {"config", {{"label", "第100期"}}},
            {"reminder", "每周五更新"},
            {"list", {
                {{"aid", 1}, {"bvid", "BV1wk0000001"}, {"cid", 1}, {"pic", ""}, {"title", "每周必看"},
                 {"duration", 60}, {"pubdate", 0}, {"owner", {{"mid", 1}, {"name", "up"}}}, {"stat", {{"view", 1}, {"danmaku", 0}}}, {"rcmd_reason", "硬核科普"}},
                {{"aid", 2}, {"bvid", "BV1wkAd00002"}, {"cid", 2}, {"pic", ""}, {"title", "商业推广"},
                 {"duration", 60}, {"pubdate", 0}, {"owner", {{"mid", 2}, {"name", "ad"}}}, {"stat", {{"view", 1}, {"danmaku", 0}}}, {"rcmd_reason", "商业推广"}}
            }}
        };
        auto parsedWeekly = weekly.get<bilibili::HotsWeeklyVideoListResultWrapper>();
        assert(parsedWeekly.list.size() == 1);
        assert(parsedWeekly.list[0].bvid == "BV1wk0000001");
    }

    // 3. Search results (including missing aid on ad cards)
    {
        json search = {
            {"page", 1}, {"pagesize", 20}, {"numResults", 3}, {"numPages", 1},
            {"result", {
                {{"type", "video"}, {"aid", 11}, {"bvid", "BV1sea000001"}, {"author", "up"}, {"duration", "05:00"},
                 {"pic", "//i0.hdslb.com/a.jpg"}, {"play", 10}, {"danmaku", 1}, {"like", 2}, {"pubdate", 0}, {"title", "正常搜索视频"}},
                {{"type", "ad"}, {"title", "搜索商业贴片"}, {"cm_info", {{"ads", {1}}}}},
                {{"type", "video"}, {"aid", 12}, {"bvid", "BV1seaAd0002"}, {"author", "ad"}, {"duration", "01:00"},
                 {"pic", ""}, {"play", 10}, {"danmaku", 1}, {"like", 2}, {"pubdate", 0}, {"title", "带 ad_info 视频"},
                 {"ad_info", {{"creative_id", 42}}}}
            }}
        };
        auto parsed = search.get<bilibili::SearchResult>();
        int supported = 0;
        for (const auto& r : parsed.result) {
            if (!r.notSupported) ++supported;
        }
        assert(supported == 1);
        assert(parsed.result[0].bvid == "BV1sea000001");
    }

    // 4. Dynamic video & article feeds
    {
        json dynVideo = {
            {"has_more", true}, {"offset", "1"}, {"update_baseline", "1"}, {"update_num", 1},
            {"items", {
                {{"aid", 1}, {"bvid", "BV1dyn000001"}, {"pic", ""}, {"title", "动态视频"}, {"duration", 30}, {"pubdate", 0},
                 {"owner", {{"mid", 1}, {"name", "up"}}}, {"stat", {{"view", 10}, {"danmaku", 0}}}},
                {{"aid", 2}, {"bvid", "BV1dynAd0002"}, {"title", "动态广告"}, {"is_ad", true}}
            }}
        };
        auto parsedDynVid = dynVideo.get<bilibili::DynamicListResultWrapper<bilibili::DynamicVideoListResult>>();
        assert(parsedDynVid.items.size() == 1);
        assert(parsedDynVid.items[0].bvid == "BV1dyn000001");

        json dynArticle = {
            {"has_more", false}, {"offset", ""}, {"update_baseline", ""}, {"update_num", 0},
            {"items", {
                {{"visible", true}, {"id_str", "100"}, {"type", "DYNAMIC_TYPE_WORD"}, {"modules", json::array()}},
                {{"visible", true}, {"id_str", "101"}, {"type", "DYNAMIC_TYPE_AD"},
                 {"modules", {{{"module_type", "MODULE_TYPE_AD"}, {"module_ad", json::object()}}}}},
                {{"visible", false}, {"id_str", "102"}, {"type", "DYNAMIC_TYPE_WORD"}, {"modules", json::array()}}
            }}
        };
        auto parsedDynArt = dynArticle.get<bilibili::DynamicListResultWrapper<bilibili::DynamicArticleListResult>>();
        assert(parsedDynArt.items.size() == 1);
        assert(parsedDynArt.items[0].id_str == "100");
    }

    // 5. Related videos & Live cards
    {
        json detailAll = {
            {"View", {{"bvid", "BV1main00001"}, {"aid", 1}, {"owner", {{"mid", 1}, {"name", "up"}}},
                      {"title", "主视频"}, {"pic", ""}, {"desc", ""}, {"pubdate", 0},
                      {"stat", {{"aid", 1}, {"view", 1}, {"danmaku", 0}, {"reply", 0}, {"favorite", 0}, {"coin", 0}, {"share", 0}, {"like", 0}}},
                      {"rights", {{"download", 1}, {"no_reprint", 0}, {"is_cooperation", 0}}}, {"copyright", 1}}},
            {"Card", {{"like_num", 1}, {"follower", 1}, {"article_count", 0}, {"archive_count", 1}, {"following", false},
                      {"card", {{"mid", "1"}, {"name", "up"}, {"sex", "保密"}, {"rank", "10000"}, {"face", ""}, {"sign", ""}}}}},
            {"Related", {
                {{"bvid", "BV1rel000001"}, {"aid", 2}, {"owner", {{"mid", 2}, {"name", "up2"}}}, {"title", "相关视频"}, {"pic", ""}, {"desc", ""}, {"pubdate", 0},
                 {"stat", {{"aid", 2}, {"view", 1}, {"danmaku", 0}, {"reply", 0}, {"favorite", 0}, {"coin", 0}, {"share", 0}, {"like", 0}}},
                 {"rights", {{"download", 1}, {"no_reprint", 0}, {"is_cooperation", 0}}}, {"copyright", 1}},
                {{"bvid", "BV1relAd0002"}, {"aid", 3}, {"is_ad", true}, {"title", "相关贴片广告"}, {"pic", ""}, {"desc", ""}, {"pubdate", 0},
                 {"owner", {{"mid", 3}, {"name", "ad"}}},
                 {"stat", {{"aid", 3}, {"view", 1}, {"danmaku", 0}, {"reply", 0}, {"favorite", 0}, {"coin", 0}, {"share", 0}, {"like", 0}}},
                 {"rights", {{"download", 0}, {"no_reprint", 0}, {"is_cooperation", 0}}}, {"copyright", 1}}
            }}
        };
        auto parsedDetail = detailAll.get<bilibili::VideoDetailAllResult>();
        assert(parsedDetail.Related.size() == 1);
        assert(parsedDetail.Related[0].bvid == "BV1rel000001");

        json live = {
            {"has_more", 1},
            {"card_list", {
                {{"card_type", "small_card_v1"}, {"card_data", {{"small_card_v1", {
                    {"roomid", 1001}, {"uid", 1}, {"title", "正常直播"}, {"uname", "主播"}, {"online", 100},
                    {"cover", ""}, {"area_name", "网游"}, {"watched_show", {{"num", 100}, {"text_small", "100"}, {"text_large", "100人看过"}}}
                }}}}},
                {{"card_type", "small_card_v1"}, {"card_data", {{"small_card_v1", {
                    {"roomid", 1002}, {"uid", 2}, {"title", "直播广告"}, {"uname", "ad"}, {"online", 1},
                    {"cover", ""}, {"area_name", ""}, {"ad_info", {{"creative_id", 9}}},
                    {"watched_show", {{"num", 0}, {"text_small", ""}, {"text_large", ""}}}
                }}}}}
            }}
        };
        auto parsedLive = live.get<bilibili::LiveResultWrapper>();
        assert(parsedLive.card_list.size() == 1);
        assert(parsedLive.card_list[0].roomid == 1001);
    }

    std::cout << "native-wiliwili ad_filter tests passed" << std::endl;
    return 0;
}

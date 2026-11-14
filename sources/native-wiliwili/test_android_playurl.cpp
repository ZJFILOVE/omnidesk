#include "utils/android_playurl.hpp"
#include "utils/quality_policy.hpp"
#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>

// The CLI also verifies the very same C++ transport used in the real player.
// Account secrets and returned media addresses are never printed.
int main(int argc, char** argv) {
    using bilibili::VideoUrlResult;
    VideoUrlResult parsed{};
    assert(!wiliwili::android::parsePlayReply({}, parsed));
    for (const std::string& bad : {std::string("\0", 1), std::string("\x0a\x7f", 2),
                                  std::string(11, char(0xff)), std::string("\x0b", 1)})
        assert(!wiliwili::android::parsePlayReply(bad, parsed));
    if (argc == 2) {
        std::ifstream input(argv[1], std::ios::binary);
        std::string raw((std::istreambuf_iterator<char>(input)), {});
        assert(wiliwili::android::parsePlayReply(raw, parsed));
        for (size_t size = 0; size < 20; ++size)
            assert(!wiliwili::android::parsePlayReply(std::string_view(raw).substr(0, size), parsed));
        VideoUrlResult original{}; original.timelength = parsed.timelength;
        original.quality = 80; original.dash.video = {{}};
        original.dash.video.front().id = 80; original.dash.video.front().base_url = "https://example.com/video";
        assert(wiliwili::quality::useAndroidStream(original, parsed));
        auto shortTrial = parsed; shortTrial.timelength = 30000;
        assert(!wiliwili::quality::useAndroidStream(original, shortTrial));
        auto noAudio = parsed; noAudio.dash.audio.clear();
        assert(!wiliwili::quality::useAndroidStream(original, noAudio));
    } else if (argc == 3 || argc == 4) {
        const int codec = argc == 4 ? std::stoi(argv[3]) : 12;
        assert(wiliwili::android::getPlayUrl("bvid", argv[1], 42391570547, 127, codec, argv[2], "", parsed));
    } else if (argc != 1) {
        return 2;
    }
    std::cout << "{\"quality\":" << wiliwili::quality::availableQuality(parsed)
              << ",\"duration_ms\":" << parsed.timelength << ",\"videos\":" << parsed.dash.video.size()
              << ",\"audios\":" << parsed.dash.audio.size() << ",\"passed\":true}" << std::endl;
}

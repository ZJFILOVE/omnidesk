#include "omnidesk/pm/scaffold.hpp"

#include <cctype>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include "omnidesk/plugin_abi.h"
#include "omnidesk/pm/manifest.hpp"
#include "omnidesk/pm/process.hpp"

namespace fs = std::filesystem;

namespace omnidesk::pm {
namespace {

std::string toIdentifier(const std::string& id) {
    std::string out;
    for (unsigned char ch : id) {
        if (std::isalnum(ch)) {
            out += static_cast<char>(ch);
        } else {
            out += '_';
        }
    }
    if (out.empty() || std::isdigit(static_cast<unsigned char>(out.front()))) {
        out = "plugin_" + out;
    }
    return out;
}

std::string toClassName(const std::string& id) {
    std::string out;
    bool cap = true;
    for (unsigned char ch : id) {
        if (std::isalnum(ch)) {
            out += static_cast<char>(cap ? std::toupper(ch) : ch);
            cap = false;
        } else {
            cap = true;
        }
    }
    if (out.empty()) {
        out = "CustomPlugin";
    }
    if (out.size() < 6 || out.substr(out.size() - 6) != "Plugin") {
        out += "Plugin";
    }
    return out;
}

bool writeTextFile(const fs::path& p, const std::string& content) {
    std::error_code ec;
    if (p.has_parent_path()) {
        fs::create_directories(p.parent_path(), ec);
        if (ec) {
            return false;
        }
    }
    std::ofstream ofs(p, std::ios::trunc);
    if (!ofs.is_open()) {
        return false;
    }
    ofs << content;
    return ofs.good();
}

} // namespace

ScaffoldResult scaffoldPluginRepo(const ScaffoldOptions& opts) {
    ScaffoldResult res;
    if (opts.target_dir.empty()) {
        res.error = "target_dir must not be empty";
        return res;
    }
    if (opts.plugin_id.empty()) {
        res.error = "plugin_id must not be empty";
        return res;
    }

    const std::string target_name = toIdentifier(opts.plugin_id);
    const std::string class_name = toClassName(opts.plugin_id);
    const std::string display_name = opts.name.empty() ? opts.plugin_id : opts.name;
    const std::string repo_url = opts.github_owner.empty()
                                     ? ("https://github.com/YOUR_ORG/" + opts.plugin_id + ".git")
                                     : ("https://github.com/" + opts.github_owner + "/" + opts.plugin_id + ".git");

    PluginManifest manifest;
    manifest.id = opts.plugin_id;
    manifest.name = display_name;
    manifest.version = opts.version.empty() ? "0.1.0" : opts.version;
    manifest.abi_version = OMNIDESK_PLUGIN_ABI_VERSION;
    manifest.kind = opts.kind;
    manifest.domain = opts.domain;
    manifest.author = opts.author.empty() ? "OmniDesk Developer" : opts.author;
    manifest.description = "Standalone decoupled OmniDesk plugin: " + display_name;
    manifest.repo_type = "git";
    manifest.repo_url = repo_url;
    manifest.repo_branch = "main";
    manifest.build_system = "cmake";
    manifest.entry_target = target_name;
    manifest.output_lib = "lib" + target_name + ".so";
    manifest.verify_cmd = "ctest --output-on-failure";
    manifest.provides = opts.provides;
    manifest.consumes = opts.consumes;

    ConfigField enabled_hint;
    enabled_hint.key = "endpoint";
    enabled_hint.label = "Endpoint / Source";
    enabled_hint.type = ConfigFieldType::String;
    enabled_hint.default_value = "";
    enabled_hint.description = "Primary service URL or local connection endpoint";
    manifest.config_schema.push_back(enabled_hint);

    std::string validate_err;
    if (!validateManifest(manifest, validate_err)) {
        res.error = validate_err;
        return res;
    }

    const fs::path root(opts.target_dir);
    std::error_code ec;
    fs::create_directories(root / "src", ec);
    fs::create_directories(root / "tests", ec);
    if (ec) {
        res.error = "Failed creating directory: " + ec.message();
        return res;
    }

    const fs::path manifest_file = root / "plugin.json";
    if (!writeTextFile(manifest_file, manifestToJson(manifest).dump(2) + "\n")) {
        res.error = "Failed writing plugin.json";
        return res;
    }
    res.created_files.push_back(manifest_file.string());

    std::string cmake_src =
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(" + target_name + " LANGUAGES CXX)\n\n"
        "set(CMAKE_CXX_STANDARD 17)\n"
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
        "set(CMAKE_POSITION_INDEPENDENT_CODE ON)\n\n"
        "find_package(OmniDeskSDK REQUIRED)\n\n"
        "add_library(" + target_name + " SHARED\n"
        "    src/plugin_main.cpp\n"
        ")\n"
        "target_link_libraries(" + target_name + " PRIVATE OmniDesk::SDK)\n"
        "set_target_properties(" + target_name + " PROPERTIES\n"
        "    OUTPUT_NAME \"" + target_name + "\"\n"
        "    PREFIX \"lib\"\n"
        ")\n\n"
        "enable_testing()\n"
        "add_executable(test_abi tests/test_abi.cpp)\n"
        "target_link_libraries(test_abi PRIVATE OmniDesk::SDK dl)\n"
        "add_test(NAME abi_smoke COMMAND test_abi $<TARGET_FILE:" + target_name + "> \"" + opts.plugin_id + "\")\n";

    const fs::path cmake_file = root / "CMakeLists.txt";
    if (!writeTextFile(cmake_file, cmake_src)) {
        res.error = "Failed writing CMakeLists.txt";
        return res;
    }
    res.created_files.push_back(cmake_file.string());

    std::string first_cap = opts.provides.empty() ? (target_name + ".ping.v1") : opts.provides.front();
    std::string cpp_src =
        "#include <omnidesk/sdk.hpp>\n\n"
        "class " + class_name + " final : public omnidesk::IPlugin {\n"
        "public:\n"
        "    bool onLoad(omnidesk::PluginContext& ctx) override {\n"
        "        ctx_ = &ctx;\n"
        "        ctx.logInfo(\"Loaded " + opts.plugin_id + "\");\n"
        "        ctx.registerCapability(\"" + first_cap + "\",\n"
        "            [this](const nlohmann::json& req) -> omnidesk::CapabilityResult {\n"
        "                omnidesk::CapabilityResult res;\n"
        "                res.ok = true;\n"
        "                res.data = {{\"plugin_id\", ctx_->pluginId()}, {\"echo\", req}};\n"
        "                return res;\n"
        "            },\n"
        "            {{\"name\", \"" + display_name + "\"}, {\"version\", \"" + manifest.version + "\"}});\n"
        "        return true;\n"
        "    }\n\n"
        "    void onUnload() override {\n"
        "        if (ctx_) {\n"
        "            ctx_->logInfo(\"Unloading " + opts.plugin_id + "\");\n"
        "            ctx_ = nullptr;\n"
        "        }\n"
        "    }\n\n"
        "    nlohmann::json onHealthCheck() const override {\n"
        "        return {{\"ok\", true}, {\"plugin_id\", \"" + opts.plugin_id + "\"}};\n"
        "    }\n\n"
        "private:\n"
        "    omnidesk::PluginContext* ctx_ = nullptr;\n"
        "};\n\n"
        "OMNIDESK_DECLARE_PLUGIN(" + class_name + ",\n"
        "                        \"" + opts.plugin_id + "\",\n"
        "                        \"" + display_name + "\",\n"
        "                        \"" + manifest.version + "\",\n"
        "                        \"" + toString(opts.kind) + "\",\n"
        "                        \"" + toString(opts.domain) + "\")\n";

    const fs::path cpp_file = root / "src" / "plugin_main.cpp";
    if (!writeTextFile(cpp_file, cpp_src)) {
        res.error = "Failed writing src/plugin_main.cpp";
        return res;
    }
    res.created_files.push_back(cpp_file.string());

    std::string test_src =
        "#include <dlfcn.h>\n"
        "#include <iostream>\n"
        "#include <string>\n"
        "#include <omnidesk/plugin_abi.h>\n\n"
        "int main(int argc, char** argv) {\n"
        "    if (argc < 3) {\n"
        "        std::cerr << \"Usage: test_abi <so_path> <expected_id>\\n\";\n"
        "        return 2;\n"
        "    }\n"
        "    void* h = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);\n"
        "    if (!h) {\n"
        "        std::cerr << \"dlopen failed: \" << dlerror() << \"\\n\";\n"
        "        return 1;\n"
        "    }\n"
        "    auto info_fn = reinterpret_cast<OmniDeskAbiInfoFn>(dlsym(h, \"omnidesk_plugin_abi_info\"));\n"
        "    auto create_fn = reinterpret_cast<OmniDeskPluginCreateFn>(dlsym(h, \"omnidesk_plugin_create\"));\n"
        "    auto destroy_fn = reinterpret_cast<OmniDeskPluginDestroyFn>(dlsym(h, \"omnidesk_plugin_destroy\"));\n"
        "    if (!info_fn || !create_fn || !destroy_fn) {\n"
        "        std::cerr << \"Missing ABI symbol\\n\";\n"
        "        dlclose(h);\n"
        "        return 1;\n"
        "    }\n"
        "    const OmniDeskPluginInfo* info = info_fn();\n"
        "    if (!info || info->abi_version != OMNIDESK_PLUGIN_ABI_VERSION ||\n"
        "        std::string(info->id) != argv[2]) {\n"
        "        std::cerr << \"ABI metadata mismatch\\n\";\n"
        "        dlclose(h);\n"
        "        return 1;\n"
        "    }\n"
        "    void* inst = create_fn();\n"
        "    if (!inst) {\n"
        "        std::cerr << \"create returned null\\n\";\n"
        "        dlclose(h);\n"
        "        return 1;\n"
        "    }\n"
        "    destroy_fn(inst);\n"
        "    dlclose(h);\n"
        "    return 0;\n"
        "}\n";

    const fs::path test_file = root / "tests" / "test_abi.cpp";
    if (!writeTextFile(test_file, test_src)) {
        res.error = "Failed writing tests/test_abi.cpp";
        return res;
    }
    res.created_files.push_back(test_file.string());

    std::string readme_src =
        "# " + display_name + "\n\n"
        "Standalone OmniDesk C++17 `.so` plugin repository (`" + opts.plugin_id + "`).\n\n"
        "## Install locally\n\n"
        "```bash\n"
        "omnidesk-pm install .\n"
        "```\n\n"
        "## Publish to GitHub\n\n"
        "Push this directory as an independent GitHub repository, then install from any OmniDesk instance:\n\n"
        "```bash\n"
        "omnidesk-pm install " + (opts.github_owner.empty() ? "YOUR_ORG" : opts.github_owner) + "/" + opts.plugin_id + "\n"
        "```\n";

    const fs::path readme_file = root / "README.md";
    if (!writeTextFile(readme_file, readme_src)) {
        res.error = "Failed writing README.md";
        return res;
    }
    res.created_files.push_back(readme_file.string());

    const fs::path gitignore_file = root / ".gitignore";
    if (!writeTextFile(gitignore_file, "build/\ncmake-build-*/\n*.user\n")) {
        res.error = "Failed writing .gitignore";
        return res;
    }
    res.created_files.push_back(gitignore_file.string());

    if (opts.init_git && !fs::exists(root / ".git")) {
        const std::string git_bin = findExecutableInPath("git");
        if (!git_bin.empty()) {
            runProcess({git_bin, "init", "-b", "main"}, root.string(), {}, 10000);
        }
    }

    res.ok = true;
    res.dir_path = fs::weakly_canonical(root, ec).string();
    if (res.dir_path.empty()) {
        res.dir_path = root.string();
    }
    return res;
}

} // namespace omnidesk::pm

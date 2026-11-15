#pragma once

#include <memory>
#include <string>
#include "omnidesk/export.h"

namespace omnidesk {

/**
 * Cross-process & intra-process advisory file lock backed by POSIX flock(2)
 * paired with an in-process per-path mutex registry.
 * Used by SecretVault, ConfigStore, PluginInstaller, and SearchIndexService
 * to prevent concurrent GUI / CLI / worker processes from clobbering shared files.
 */
class OMNIDESK_API FileLock {
public:
    enum class Mode {
        Exclusive,
        Shared,
    };

    explicit FileLock(std::string lock_path, Mode mode = Mode::Exclusive);
    ~FileLock();

    FileLock(const FileLock&) = delete;
    FileLock& operator=(const FileLock&) = delete;
    FileLock(FileLock&&) noexcept;
    FileLock& operator=(FileLock&&) noexcept;

    bool lock();
    bool tryLock();
    bool lockFor(int timeout_ms);
    void unlock();

    bool isLocked() const noexcept { return locked_; }
    const std::string& lockPath() const noexcept { return lock_path_; }

private:
    struct ProcessEntry;

    static std::shared_ptr<ProcessEntry> lookupProcessEntry(const std::string& key);
    bool acquireFlock(bool non_blocking);
    void releaseFlock();

    std::string lock_path_;
    Mode mode_ = Mode::Exclusive;
    std::shared_ptr<ProcessEntry> entry_;
    bool owns_inproc_ = false;
    int fd_ = -1;
    bool locked_ = false;
};

/**
 * RAII guard acquiring a FileLock for the current scope.
 */
class OMNIDESK_API ScopedFileLock {
public:
    explicit ScopedFileLock(const std::string& target_path,
                            FileLock::Mode mode = FileLock::Mode::Exclusive,
                            int timeout_ms = 5000);
    ~ScopedFileLock();

    ScopedFileLock(const ScopedFileLock&) = delete;
    ScopedFileLock& operator=(const ScopedFileLock&) = delete;

    bool ownsLock() const noexcept { return acquired_; }

private:
    FileLock lock_;
    bool acquired_ = false;
};

} // namespace omnidesk

#include "omnidesk/file_lock.hpp"

#include <cerrno>
#include <chrono>
#include <fcntl.h>
#include <filesystem>
#include <mutex>
#include <sys/file.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <unordered_map>

namespace fs = std::filesystem;

namespace omnidesk {

struct FileLock::ProcessEntry {
    std::timed_mutex mutex;
};

namespace {

std::string normalizeLockPath(const std::string& raw) {
    if (raw.empty()) return {};
    std::error_code ec;
    fs::path p(raw);
    if (p.is_relative()) {
        p = fs::absolute(p, ec);
    }
    return p.lexically_normal().string();
}

} // namespace

std::shared_ptr<FileLock::ProcessEntry> FileLock::lookupProcessEntry(const std::string& key) {
    static std::mutex reg_mutex;
    static auto* reg = new std::unordered_map<std::string, std::weak_ptr<FileLock::ProcessEntry>>();
    std::lock_guard<std::mutex> lk(reg_mutex);
    auto it = reg->find(key);
    if (it != reg->end()) {
        if (auto existing = it->second.lock()) {
            return existing;
        }
    }
    auto entry = std::make_shared<FileLock::ProcessEntry>();
    (*reg)[key] = entry;
    return entry;
}

FileLock::FileLock(std::string lock_path, Mode mode)
    : lock_path_(normalizeLockPath(lock_path)),
      mode_(mode),
      entry_(lock_path_.empty() ? nullptr : lookupProcessEntry(lock_path_)) {}

FileLock::~FileLock() {
    unlock();
}

FileLock::FileLock(FileLock&& other) noexcept
    : lock_path_(std::move(other.lock_path_)),
      mode_(other.mode_),
      entry_(std::move(other.entry_)),
      owns_inproc_(other.owns_inproc_),
      fd_(other.fd_),
      locked_(other.locked_) {
    other.owns_inproc_ = false;
    other.fd_ = -1;
    other.locked_ = false;
}

FileLock& FileLock::operator=(FileLock&& other) noexcept {
    if (this != &other) {
        unlock();
        lock_path_ = std::move(other.lock_path_);
        mode_ = other.mode_;
        entry_ = std::move(other.entry_);
        owns_inproc_ = other.owns_inproc_;
        fd_ = other.fd_;
        locked_ = other.locked_;
        other.owns_inproc_ = false;
        other.fd_ = -1;
        other.locked_ = false;
    }
    return *this;
}

bool FileLock::acquireFlock(bool non_blocking) {
    if (lock_path_.empty()) return false;
    std::error_code ec;
    fs::path p(lock_path_);
    if (p.has_parent_path()) {
        fs::create_directories(p.parent_path(), ec);
        if (ec) return false;
        ::chmod(p.parent_path().c_str(), 0700);
    }

    int fd = ::open(lock_path_.c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (fd < 0) {
        return false;
    }
    ::fchmod(fd, 0600);

    int op = (mode_ == Mode::Exclusive) ? LOCK_EX : LOCK_SH;
    if (non_blocking) op |= LOCK_NB;

    while (::flock(fd, op) != 0) {
        if (errno == EINTR && !non_blocking) {
            continue;
        }
        ::close(fd);
        return false;
    }
    fd_ = fd;
    locked_ = true;
    return true;
}

void FileLock::releaseFlock() {
    if (fd_ >= 0) {
        ::flock(fd_, LOCK_UN);
        ::close(fd_);
        fd_ = -1;
    }
    locked_ = false;
}

bool FileLock::lock() {
    if (locked_) return true;
    if (!entry_) return false;
    entry_->mutex.lock();
    owns_inproc_ = true;
    if (!acquireFlock(false)) {
        owns_inproc_ = false;
        entry_->mutex.unlock();
        return false;
    }
    return true;
}

bool FileLock::tryLock() {
    if (locked_) return true;
    if (!entry_) return false;
    if (!entry_->mutex.try_lock()) {
        return false;
    }
    owns_inproc_ = true;
    if (!acquireFlock(true)) {
        owns_inproc_ = false;
        entry_->mutex.unlock();
        return false;
    }
    return true;
}

bool FileLock::lockFor(int timeout_ms) {
    if (locked_) return true;
    if (!entry_) return false;
    if (timeout_ms < 0) return lock();

    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    if (!entry_->mutex.try_lock_until(deadline)) {
        return false;
    }
    owns_inproc_ = true;

    while (true) {
        if (acquireFlock(true)) {
            return true;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            owns_inproc_ = false;
            entry_->mutex.unlock();
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
}

void FileLock::unlock() {
    releaseFlock();
    if (owns_inproc_ && entry_) {
        owns_inproc_ = false;
        entry_->mutex.unlock();
    }
}

ScopedFileLock::ScopedFileLock(const std::string& target_path,
                               FileLock::Mode mode,
                               int timeout_ms)
    : lock_(target_path.empty() ? std::string{} : (target_path + ".lock"), mode) {
    acquired_ = (timeout_ms < 0) ? lock_.lock() : lock_.lockFor(timeout_ms);
}

ScopedFileLock::~ScopedFileLock() {
    if (acquired_) {
        lock_.unlock();
        acquired_ = false;
    }
}

} // namespace omnidesk

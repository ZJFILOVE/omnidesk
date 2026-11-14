#include "omnidesk/pm/process.hpp"

#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sstream>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace omnidesk::pm {

std::string findExecutableInPath(const std::string& name) {
    if (name.empty()) {
        return "";
    }
    if (name.find('/') != std::string::npos) {
        if (access(name.c_str(), X_OK) == 0) {
            return name;
        }
        return "";
    }
    const char* path_env = std::getenv("PATH");
    if (!path_env) {
        return "";
    }
    std::stringstream ss(path_env);
    std::string dir;
    while (std::getline(ss, dir, ':')) {
        if (dir.empty()) {
            continue;
        }
        std::string candidate = dir;
        if (candidate.back() != '/') {
            candidate += '/';
        }
        candidate += name;
        if (access(candidate.c_str(), X_OK) == 0) {
            return candidate;
        }
    }
    return "";
}

ProcessResult runProcess(const std::vector<std::string>& argv,
                         const std::string& cwd,
                         const std::map<std::string, std::string>& extra_env,
                         int timeout_ms,
                         OutputCallback on_output) {
    ProcessResult res;
    if (argv.empty() || argv[0].empty()) {
        res.error = "Empty command argv";
        return res;
    }

    int pipefd[2];
    if (pipe(pipefd) != 0) {
        res.error = std::string("pipe() failed: ") + std::strerror(errno);
        return res;
    }

    // Close write end on exec? No, child writes to pipefd[1] via stdout/stderr.
    // Set non-blocking on read end.
    int flags = fcntl(pipefd[0], F_GETFL, 0);
    if (flags >= 0) {
        fcntl(pipefd[0], F_SETFL, flags | O_NONBLOCK);
    }

    pid_t pid = fork();
    if (pid < 0) {
        close(pipefd[0]);
        close(pipefd[1]);
        res.error = std::string("fork() failed: ") + std::strerror(errno);
        return res;
    }

    if (pid == 0) {
        setpgid(0, 0);
        // Child process
        close(pipefd[0]);
        dup2(pipefd[1], STDOUT_FILENO);
        dup2(pipefd[1], STDERR_FILENO);
        if (pipefd[1] != STDOUT_FILENO && pipefd[1] != STDERR_FILENO) {
            close(pipefd[1]);
        }

        // Redirect stdin to /dev/null so child cannot block waiting on terminal input
        int devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) {
            dup2(devnull, STDIN_FILENO);
            close(devnull);
        }

        if (!cwd.empty()) {
            if (chdir(cwd.c_str()) != 0) {
                const char* msg = "Failed to chdir to working directory\n";
                (void)write(STDERR_FILENO, msg, std::strlen(msg));
                _exit(126);
            }
        }

        for (const auto& [k, v] : extra_env) {
            setenv(k.c_str(), v.c_str(), 1);
        }

        std::vector<char*> c_argv;
        c_argv.reserve(argv.size() + 1);
        for (const auto& arg : argv) {
            c_argv.push_back(const_cast<char*>(arg.c_str()));
        }
        c_argv.push_back(nullptr);

        execvp(argv[0].c_str(), c_argv.data());
        const int err = errno;
        std::string errmsg = "execvp(" + argv[0] + ") failed: " + std::strerror(err) + "\n";
        (void)write(STDERR_FILENO, errmsg.data(), errmsg.size());
        _exit(err == ENOENT ? 127 : 126);
    }

    // Parent process
    setpgid(pid, pid);
    close(pipefd[1]);

    auto start = std::chrono::steady_clock::now();
    bool pipe_open = true;
    int status = 0;
    bool waited = false;

    char buf[4096];
    while (pipe_open || !waited) {
        int remaining_ms = -1;
        if (timeout_ms > 0) {
            auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                               std::chrono::steady_clock::now() - start)
                               .count();
            if (elapsed >= timeout_ms) {
                res.timed_out = true;
                kill(-pid, SIGKILL);
                break;
            }
            remaining_ms = static_cast<int>(timeout_ms - elapsed);
            if (remaining_ms > 200) {
                remaining_ms = 200;
            }
        } else {
            remaining_ms = 200;
        }

        struct pollfd pfd {};
        pfd.fd = pipe_open ? pipefd[0] : -1;
        pfd.events = POLLIN | POLLHUP;
        int pr = poll(&pfd, 1, remaining_ms);
        if (pr < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (pr > 0) {
            for (size_t batch = 0; batch < 256; ++batch) {
                ssize_t n = read(pipefd[0], buf, sizeof(buf));
                if (n > 0) {
                    std::string chunk(buf, static_cast<size_t>(n));
                    if (res.output.size() < 8 * 1024 * 1024) res.output += chunk;
                    if (on_output) {
                        on_output(chunk);
                    }
                } else if (n == 0) {
                    pipe_open = false;
                    break;
                } else {
                    if (errno == EAGAIN || errno == EWOULDBLOCK) {
                        break;
                    }
                    if (errno == EINTR) {
                        continue;
                    }
                    pipe_open = false;
                    break;
                }
            }
        }

        // Non-blocking check if child already exited & pipe closed on next iteration
        pid_t w = waitpid(pid, &status, WNOHANG);
        if (w == pid) {
            waited = true;
            // Drain any remaining bytes from pipe
            for (size_t batch = 0; batch < 256; ++batch) {
                ssize_t n = read(pipefd[0], buf, sizeof(buf));
                if (n > 0) {
                    std::string chunk(buf, static_cast<size_t>(n));
                    if (res.output.size() < 8 * 1024 * 1024) res.output += chunk;
                    if (on_output) {
                        on_output(chunk);
                    }
                } else {
                    break;
                }
            }
            pipe_open = false;
        }
    }

    close(pipefd[0]);
    kill(-pid, SIGKILL);

    if (!waited) {
        while (waitpid(pid, &status, 0) < 0) {
            if (errno != EINTR) {
                res.error = std::string("waitpid() failed: ") + std::strerror(errno);
                return res;
            }
        }
    }

    if (res.timed_out) {
        res.exit_code = -1;
        res.error = "Process timed out";
        return res;
    }

    if (WIFEXITED(status)) {
        res.exit_code = WEXITSTATUS(status);
        if (res.exit_code != 0 && res.error.empty()) {
            res.error = "Process exited with code " + std::to_string(res.exit_code);
        }
    } else if (WIFSIGNALED(status)) {
        int sig = WTERMSIG(status);
        res.exit_code = 128 + sig;
        res.error = "Process terminated by signal " + std::to_string(sig);
    }

    return res;
}

} // namespace omnidesk::pm

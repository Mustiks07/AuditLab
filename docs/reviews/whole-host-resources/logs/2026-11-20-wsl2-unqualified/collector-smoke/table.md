| Показатель | Байт | Примечание |
|---|---:|---|
| MemTotal гостевой ОС | 7 945 748 480 | из /proc/meminfo |
| SwapTotal | 2 147 483 648 | должно быть 0 |
| Пик used (MemTotal − MemAvailable), sampled | 593 866 752 | интервал 0.25 с |
| Пик used вместе с кешем, sampled | 1 074 302 976 | MemTotal − MemFree |
| Минимум MemAvailable, sampled | 7 351 881 728 |  |
| Пик swap used, sampled | 0 |  |
| OOM kill (vmstat, прирост) | 0 | за весь интервал записи |
| Длительность записи, с | 3.003 | 14 сэмплов, max gap 0.2509999999999999 |
| system.slice: memory.peak (непрерывный пик этой cgroup) | 346 509 312 | oom_kill +0 |
| user.slice: memory.peak (непрерывный пик этой cgroup) | 19 259 392 | oom_kill +0 |
| init.scope: memory.peak (непрерывный пик этой cgroup) | 4 698 112 | oom_kill +0 |

Границы вывода:

- Host figures are sampled every interval_s; a shorter spike can be missed. They are not a continuous maximum.
- continuous_peak_bytes comes from one cgroup's memory.peak; peaks of different cgroups need not coincide, so their sum is not the host peak and is never reported as such.
- A container's memory.peak is lost when its cgroup is removed; last_seen_continuous_peak_bytes is the last value read while it existed and can be below its true final peak.
- used_excluding_reclaimable = MemTotal - MemAvailable (kernel estimate). used_including_cache adds page cache.
- MemTotal is what the guest kernel reports; it is below the RAM assigned by the hypervisor.

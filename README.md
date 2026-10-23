# remote desktop

CTFd plugin for Docker desktops.

Install at `CTFd/plugins/ctfd-remote-desktop`, then run from the CTFd root:

```sh
bash CTFd/plugins/ctfd-remote-desktop/setup.sh
docker compose up -d --build
```

Configure and enable under `/admin/config`.

- Browser desktop, terminal and SSH
- Local and SSH Docker runners
- Usage metrics and tutor context
- Session limits and admin controls

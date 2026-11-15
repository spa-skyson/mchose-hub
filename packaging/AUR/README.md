# Публикация в AUR

AUR принимает пакеты только от зарегистрированных пользователей с SSH-ключом: зарегистрируйтесь на https://aur.archlinux.org/ и добавьте публичный ключ в профиль.

Для `mchose-hub-git`:

```bash
git clone ssh://aur@aur.archlinux.org/mchose-hub-git.git
cd mchose-hub-git
cp /путь/к/MCHOSE-hub/packaging/AUR/mchose-hub-git/PKGBUILD .
namcap PKGBUILD
makepkg -f
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO
git commit -m 'Add mchose-hub-git package'
git push
```

Проверяйте `namcap PKGBUILD` и `makepkg -f` локально до публикации. `.SRCINFO` создавайте в клоне AUR, а не в этом проекте. `mksrcinfo` устарел: используйте `makepkg --printsrcinfo`. Для стабильного пакета повторите шаги с репозиторием `mchose-hub.git` и `mchose-hub/PKGBUILD` после выпуска тега; обновите `pkgver` и SHA-256 архива.

Зависимости: `python-pyqt6` есть в официальном репозитории Arch Linux; опциональный `noise-suppression-for-voice` — в AUR.

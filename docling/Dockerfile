# 固定官方 CPU 镜像及内置 OCR/版面/表格模型；旧版 Office 由 LibreOffice 转换。
FROM quay.io/docling-project/docling-serve-cpu@sha256:2219f164c5f2969f3c8a7df4930a2d1d20ac0208f655fd4ad2fe468fd32526bd
USER 0
RUN --mount=type=cache,target=/var/cache/dnf \
    dnf --setopt=appstream.metalink= \
    --setopt='appstream.baseurl=https://mirror.stream.centos.org/9-stream/AppStream/$basearch/os/' \
    install -y --setopt=install_weak_deps=False libreoffice-writer libreoffice-calc
USER 1001

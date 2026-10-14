#!/usr/bin/python3

from __future__ import annotations
import sys
import unicodedata
import datetime
import os
import glob
import subprocess
from absl import app
from absl import flags

from rootfs_utils import scoped_tmpdir_tegraflash_rootfs, Filesystem, check_required_deps, check_buildifier, scoped_mount, generate_build_file, detect_yocto_codename

REQUIRED_DEPS = ["xfsprogs"]


def _detect_cuda_version(partition):
    """Auto-detect the CUDA version installed under /usr/local in the
    target rootfs. Yocto installs CUDA as `usr/local/cuda-XX.Y/`. Returns
    the version string (e.g. '13.2') or None if no CUDA install is found.
    """
    usr_local = os.path.join(partition, "usr", "local")
    if not os.path.isdir(usr_local):
        return None
    versions = []
    for name in os.listdir(usr_local):
        if name.startswith("cuda-"):
            versions.append(name[len("cuda-"):])
    if len(versions) != 1:
        print(
            f"WARNING: expected exactly one /usr/local/cuda-XX.Y directory, "
            f"found {len(versions)}: {versions}",
            file=sys.stderr)
        if not versions:
            return None
    return sorted(versions)[-1]


def _find_native_cuda_bin(yocto_root, cuda_version):
    """Find the unmodified ptxas/fatbinary source directory in the Yocto
    build tree. Yocto unpacks upstream CUDA toolchain binaries into
    `build/tmp/sysroots-components/aarch64_tegra/cuda-nvcc/usr/local/cuda-XX.Y/bin/`
    without baking uninative paths into the ELF interpreter, which is why
    we reinject these copies after excluding the path-baked copies that
    landed in the target rootfs. Returns the bin/ path or None.
    """
    # Try to pin the exact version; fall back to a glob if the exact
    # directory isn't present (Yocto sometimes appends a build-id suffix).
    patterns = [
        f"usr/local/cuda-{cuda_version}/bin",
        f"usr/local/cuda-{cuda_version}*/bin",
    ]
    base = os.path.join(
        yocto_root,
        "build/tmp/sysroots-components/aarch64_tegra/cuda-nvcc")
    for pattern in patterns:
        for match in sorted(glob.glob(os.path.join(base, pattern))):
            if os.path.isfile(os.path.join(match, "ptxas")) and \
               os.path.isfile(os.path.join(match, "fatbinary")):
                return match
    return None


def do_package(yocto_root, partition):
    codename = detect_yocto_codename(yocto_root)
    if not codename:
        print(
            "WARNING: could not auto-detect Yocto DISTRO_CODENAME; "
            "falling back to the historical name 'wrynose' in the "
            "output filename. Set DISTRO_CODENAME in your distro conf or "
            "build/conf/local.conf to silence this.",
            file=sys.stderr)
        codename = "wrynose"
    tarball = datetime.date.today().strftime(
        f"{os.getcwd()}/%Y-%m-%d-{codename}-arm64-nvidia-rootfs.tar")
    print(tarball, file=sys.stderr)

    cuda_version = _detect_cuda_version(partition)
    if not cuda_version:
        raise RuntimeError(
            "Could not auto-detect CUDA version in the target rootfs "
            f"(looked for usr/local/cuda-XX.Y under {partition}).")
    cuda_dir = f"usr/local/cuda-{cuda_version}"
    print(f"Detected CUDA {cuda_version} in target rootfs", file=sys.stderr)

    subprocess.run([
        "sudo",
        "tar",
        "--sort=name",
        "--mtime=0",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--exclude=./usr/share/ca-certificates",
        "--exclude=./home",
        "--exclude=./root",
        "--exclude=./usr/src",
        "--exclude=./usr/lib/mesa-diverted",
        "--exclude=./usr/bin/X11",
        "--exclude=./usr/lib/systemd/system/system-systemd*cryptsetup.slice",
        "--exclude=./dev",
        f"--exclude=./{cuda_dir}/bin/fatbinary",
        f"--exclude=./{cuda_dir}/bin/ptxas",
        f"--exclude=./{cuda_dir}/include/thrust",
        f"--exclude=./{cuda_dir}/include/nv",
        f"--exclude=./{cuda_dir}/include/cuda",
        f"--exclude=./{cuda_dir}/include/cub",
        "--exclude=./usr/include/cub",
        "--exclude=./usr/include/nv",
        "--exclude=./usr/include/thrust",
        "--exclude=./usr/include/cuda",
        "--exclude=./usr/share",
        "-cf",
        tarball,
        ".",
    ],
                   cwd=partition,
                   check=True)

    # Pull ptxas and fatbinary from yocto.
    # Note, we need to use the downloaded but unmodified ones, otherwise yocto
    # modifies the binary and bakes paths in.
    #
    # To find the right binaries, run in build/tmp in yocto:
    #  (for line in $(find | grep ptxas$); do echo $line; file $line; ldd $line; done)
    cuda_bin = _find_native_cuda_bin(yocto_root, cuda_version)
    if cuda_bin is None:
        raise RuntimeError(
            f"Could not find unmodified ptxas/fatbinary in {yocto_root} "
            f"for CUDA {cuda_version}. Looked under "
            f"build/tmp/sysroots-components/aarch64_tegra/cuda-nvcc/.")

    subprocess.run([
        "sudo",
        "tar",
        "--sort=name",
        "--mtime=0",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        f'--transform=s|{cuda_bin[1:]}/ptxas|{cuda_dir}/bin/ptxas|',
        f'--transform=s|{cuda_bin[1:]}/fatbinary|{cuda_dir}/bin/aarch64-unknown-linux-gnu-fatbinary|',
        "--append",
        "-f",
        tarball,
        os.path.join(cuda_bin, "ptxas"),
        os.path.join(cuda_bin, "fatbinary"),
    ],
                   check=True)

    subprocess.run(["sha256sum", tarball], check=True)


def main(argv):
    check_required_deps(REQUIRED_DEPS)

    if len(argv) != 2:
        print("Usage:", file=sys.stderr)
        print(" buildify_yocto_image /path/to/yocto", file=sys.stderr)
        return 1

    if argv[1][0] != '/':
        print("Path must be absolute", file=sys.stderr)
        return 1

    full_image = os.path.join(
        argv[1],
        "build/demo-image-base-p3768-0000-p3767-0003.rootfs.tegraflash-tar.zst"
    )

    if not check_buildifier():
        print(
            "ERROR: Need to have buildifier in the path.  Please resolve this.",
            file=sys.stderr)
        exit()

    if not os.path.exists(full_image):
        print("ERROR: Point to a valid image", file=sys.stderr)
        exit()

    with scoped_tmpdir_tegraflash_rootfs(full_image) as rootfs:
        with scoped_mount(rootfs) as partition:
            subprocess.run([
                "sudo", "cp", "/usr/bin/qemu-aarch64-static",
                f"{partition}/usr/bin/"
            ],
                           check=True)

            subprocess.run(["sudo", "chmod", "a+X", "-R", f"{partition}/"],
                           check=True)

            filesystem = Filesystem(
                partition,
                [
                    "sudo", "chroot", "--userspec=0:0", partition,
                    "qemu-aarch64-static", "/bin/bash"
                ],
            )

            packages_to_eval = [
                filesystem.packages['libglib-2.0-dev'],
                filesystem.packages['opencv-dev'],
                filesystem.packages['opencv'],
                filesystem.packages['tensorrt-core'],
                filesystem.packages['tensorrt-core-dev'],
                filesystem.packages['libz-dev'],
                filesystem.packages['libc6-dev'],
                filesystem.packages['xz-dev'],
                filesystem.packages['libstdc++-dev'],
                filesystem.packages['libffi-dev'],
                filesystem.packages['util-linux-dev'],
                filesystem.packages['libnpp-dev'],
                filesystem.packages['cuda-cudart-dev'],
                filesystem.packages['gstreamer1.0-dev'],
                filesystem.packages['orc-dev'],
                filesystem.packages['libgstrtp-1.0-0'],
                filesystem.packages['libpcre2-dev'],
                filesystem.packages['gstreamer1.0-plugins-bad-dev'],
                filesystem.packages['libjpeg-dev'],
            ]

            # TODO(austin): We will need to make new module versions each time we make a new sysroot.
            with open(
                    "../registry/modules/arm64_debian_sysroot/2026.10.16/overlay/BUILD.bazel",
                    "w") as file:
                file.write(
                    generate_build_file(filesystem, packages_to_eval,
                                        "orin_debian_rootfs.BUILD.template"))

            subprocess.run(['buildifier', "orin_debian_rootfs.BUILD"])

            do_package(argv[1], partition)


if __name__ == '__main__':
    app.run(main)

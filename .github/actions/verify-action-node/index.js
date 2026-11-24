const [major, minor, patch] = process.versions.node.split('.').map(Number);
console.log(`GitHub Actions runtime: ${process.version}`);
if (major !== 24 || minor < 22 || (minor === 22 && patch < 1)) {
  console.error('GitHub Actions requires patched Node 24.22.1 or later in the Node 24 series.');
  process.exitCode = 1;
}

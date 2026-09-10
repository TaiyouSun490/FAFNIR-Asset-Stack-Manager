// SPDX-License-Identifier: MIT
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;

namespace Stackforge.Editor
{
    // Runtime evidence, not a claim that merely installing source files compiled it.
    internal static class FafnirBridgeDiagnostics
    {
        private const string SyncHashKey = "Fafnir.Bridge.SyncHash";
        private const string SyncCountKey = "Fafnir.Bridge.SyncCount";
        private const string SyncTimeKey = "Fafnir.Bridge.SyncUtc";
        private static string _contentHash;
        private static string _version;

        internal static string RootPath
        {
            get
            {
                string configured = Environment.GetEnvironmentVariable("FAFNIR_BRIDGE_ROOT");
                if (!string.IsNullOrEmpty(configured))
                {
                    if (!Path.IsPathRooted(configured))
                        throw new InvalidOperationException("FAFNIR_BRIDGE_ROOT must be absolute.");
                    return Path.GetFullPath(configured);
                }
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "game-stack-planner");
            }
        }

        internal static string Version
        {
            get
            {
                EnsurePackageIdentity();
                return _version ?? "";
            }
        }

        internal static string ContentHash
        {
            get
            {
                EnsurePackageIdentity();
                return _contentHash ?? "";
            }
        }

        private static void EnsurePackageIdentity()
        {
            if (_contentHash != null) return;
            try
            {
                var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                    typeof(FafnirBridgeDiagnostics).Assembly);
                if (package == null) return;
                var root = Path.GetFullPath(package.resolvedPath).TrimEnd('/', '\\');
                var files = Directory.GetFiles(root, "*", SearchOption.AllDirectories)
                    .Select(path => new { path, relative = path.Substring(root.Length + 1).Replace('\\', '/') })
                    .OrderBy(file => file.relative, StringComparer.Ordinal).ToArray();
                if (files.Length > 128) return;
                var text = new StringBuilder();
                using (var sha = SHA256.Create())
                {
                    foreach (var file in files)
                    {
                        if (new FileInfo(file.path).Length > 4 * 1024 * 1024) return;
                        text.Append(file.relative).Append('\0')
                            .Append(Hex(sha.ComputeHash(File.ReadAllBytes(file.path)))).Append('\n');
                    }
                    _contentHash = Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(text.ToString())));
                }
                _version = package.version;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }

        private static string Hex(byte[] bytes)
        {
            return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
        }

        internal static string CompilationState
        {
            get
            {
                if (EditorApplication.isCompiling || EditorApplication.isUpdating) return "compiling";
                // No undocumented signature is assumed to exist. Unknown is not Passed.
                const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;
                try
                {
                    var property = typeof(EditorUtility).GetProperty("scriptCompilationFailed", flags);
                    var field = typeof(EditorUtility).GetField("scriptCompilationFailed", flags);
                    object result = property != null ? property.GetValue(null, null)
                        : field != null ? field.GetValue(null) : null;
                    return result is bool ? ((bool)result ? "failed" : "passed") : "unknown";
                }
                catch (Exception) { return "unknown"; }
            }
        }

        internal static void RecordSyncSuccess(int count)
        {
            SessionState.SetString(SyncHashKey, ContentHash);
            SessionState.SetInt(SyncCountKey, count);
            SessionState.SetString(SyncTimeKey, DateTime.UtcNow.ToString("O"));
        }

        internal static string SyncState
        {
            get
            {
                if (StackforgeMyAssetsSync.isRunning) return "running";
                if (StackforgeMyAssetsSync.messageType == MessageType.Error) return "failed";
                return ContentHash.Length > 0 && SessionState.GetString(SyncHashKey, "") == ContentHash
                    ? "succeeded" : "not_run";
            }
        }

        internal static int SyncCount { get { return SyncState == "succeeded" ? SessionState.GetInt(SyncCountKey, -1) : -1; } }
        internal static string SyncTime { get { return SyncState == "succeeded" ? SessionState.GetString(SyncTimeKey, "") : ""; } }
    }
}

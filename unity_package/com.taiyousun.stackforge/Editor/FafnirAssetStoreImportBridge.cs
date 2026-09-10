// SPDX-License-Identifier: MIT
using System;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEngine;

namespace Stackforge.Editor
{
    [InitializeOnLoad]
    internal static class FafnirAssetStoreImportBridge
    {
        private const string SessionKey = "Fafnir.ActiveImport";
        private static readonly string Root = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "game-stack-planner");
        private static readonly string CommandPath = Path.Combine(Root, "unity-import-command.json");
        private static Command _active;
        private static double _nextPoll;
        internal static bool IsBusy { get { return _active != null; } }

        static FafnirAssetStoreImportBridge()
        {
            string saved = SessionState.GetString(SessionKey, "");
            if (!string.IsNullOrEmpty(saved))
            {
                try { _active = JsonUtility.FromJson<Command>(saved); }
                catch { SessionState.EraseString(SessionKey); }
            }
            EditorApplication.update += Update;
            AssetDatabase.importPackageStarted += Started;
            AssetDatabase.importPackageCompleted += Completed;
            AssetDatabase.importPackageCancelled += Cancelled;
            AssetDatabase.importPackageFailed += Failed;
        }

        private static void Update()
        {
            if (_active != null || EditorApplication.isCompiling || EditorApplication.isUpdating
                || EditorApplication.timeSinceStartup < _nextPoll) return;
            _nextPoll = EditorApplication.timeSinceStartup + 1;
            if (!File.Exists(CommandPath)) return;
            try
            {
                if (new FileInfo(CommandPath).Length > 1024 * 1024) return;
                Command request = JsonUtility.FromJson<Command>(File.ReadAllText(CommandPath));
                if (request == null || request.schema != "fafnir.asset-store-import-command.v1"
                    || !Regex.IsMatch(request.jobId ?? "", "^[0-9a-f]{32}$")) return;
                string project = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
                StringComparison comparison = Application.platform == RuntimePlatform.WindowsEditor
                    ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
                if (!string.Equals(project.TrimEnd('/', '\\'),
                    Path.GetFullPath(request.projectPath).TrimEnd('/', '\\'), comparison)) return;
                _active = request;
                File.Delete(CommandPath);
                DateTime expires;
                if (!DateTime.TryParse(request.expiresAtUtc, CultureInfo.InvariantCulture,
                    DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out expires)
                    || expires <= DateTime.UtcNow)
                    throw new InvalidDataException("インポート要求が期限切れです。再試行してください。");
                if (!File.Exists(request.packagePath)
                    || !request.packagePath.EndsWith(".unitypackage", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("ダウンロード済みパッケージが見つかりません。");
                string digest;
                using (SHA256 sha = SHA256.Create())
                using (FileStream stream = File.OpenRead(request.packagePath))
                    digest = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
                if (digest != request.sha256)
                    throw new InvalidDataException("検査後にパッケージが変更されました。再検査してください。");
                SessionState.SetString(SessionKey, JsonUtility.ToJson(_active));
                Save("awaiting_unity_confirmation", "Unityのファイル一覧でImportまたはCancelを選んでください。");
                AssetDatabase.ImportPackage(request.packagePath, true);
            }
            catch (Exception exception)
            {
                if (_active != null) Finish("failed", exception.Message);
                else Debug.LogWarning("Fafnir import request: " + exception.Message);
            }
        }

        private static bool Matches(string packageName)
        {
            return _active != null && string.Equals(Path.GetFileNameWithoutExtension(packageName),
                Path.GetFileNameWithoutExtension(_active.packagePath), StringComparison.OrdinalIgnoreCase);
        }
        private static void Started(string name)
        {
            if (Matches(name)) Save("importing", "Unityへインポート中…");
        }
        private static void Completed(string name)
        {
            if (Matches(name)) Finish("imported", "インポート完了。Unityのコンパイル結果を確認してください。");
        }
        private static void Cancelled(string name)
        {
            if (Matches(name)) Finish("cancelled", "インポートをキャンセルしました。キャッシュは利用できます。");
        }
        private static void Failed(string name, string error)
        {
            if (Matches(name)) Finish("failed", error);
        }
        private static void Finish(string state, string message)
        {
            Save(state, message);
            _active = null;
            SessionState.EraseString(SessionKey);
        }
        private static void Save(string state, string message)
        {
            try
            {
                string directory = Path.Combine(Root, "unity-import-jobs");
                Directory.CreateDirectory(directory);
                string path = Path.Combine(directory, _active.jobId + ".json");
                var status = new Status { id = _active.jobId, state = state, message = message,
                    projectPath = _active.projectPath, expiresAtUtc = _active.expiresAtUtc,
                    updatedAtUtc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture) };
                File.WriteAllText(path + ".tmp", JsonUtility.ToJson(status), new UTF8Encoding(false));
                if (File.Exists(path)) File.Replace(path + ".tmp", path, null);
                else File.Move(path + ".tmp", path);
            }
            catch (IOException exception) { Debug.LogWarning("Fafnir import status: " + exception.Message); }
        }

        [Serializable] private sealed class Command
        {
            public string schema, jobId, projectPath, packagePath, sha256, expiresAtUtc;
        }
        [Serializable] private sealed class Status
        {
            public string id, state, message, projectPath, updatedAtUtc, expiresAtUtc;
        }
    }
}

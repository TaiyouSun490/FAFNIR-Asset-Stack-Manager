// SPDX-License-Identifier: MIT
// Uses Unity Editor's own authenticated Package Manager service via reflection.
// No account credential or session value leaves the Editor process.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using UnityEditor;
using UnityEngine;

namespace Stackforge.Editor
{
    [InitializeOnLoad]
    internal static class FafnirAssetStoreDownloadBridge
    {
        private const string InternalNamespace = "UnityEditor.PackageManager.UI.Internal";
        private const string CommandSchema = "fafnir.asset-store-download-command.v1";
        private const string StatusSchema = "fafnir.asset-store-download-status.v1";
        private const string BridgeSchema = "fafnir.asset-store-download-bridge.v1";
        private const int MaxProducts = 20;
        private const int MaxCommandBytes = 1024 * 1024;
        private const int CompletedState = 64;
        private const int AbortedState = 256;
        private const int ErrorState = 1024;
        private const int JsonWriteAttempts = 6;

        private static readonly string RootPath = FafnirBridgeDiagnostics.RootPath;
        private static readonly string CommandPath = Path.Combine(
            RootPath, "unity-download-command.json");
        private static readonly string StatusPath = Path.Combine(
            RootPath, "unity-download-status.json");
        private static readonly string BridgePath = Path.Combine(
            RootPath, "unity-download-bridge.json");

        private static double _nextCommandCheck;
        private static double _nextHeartbeat;
        private static object _downloadManager;
        private static MethodInfo _download;
        private static MethodInfo _getOperation;
        private static DownloadCommand _active;
        private static DateTime _activeStartedUtc;
        private static bool _authenticationFailed;
        private static readonly Dictionary<long, ProductStatus> LastProducts =
            new Dictionary<long, ProductStatus>();

        static FafnirAssetStoreDownloadBridge()
        {
            // CI/temporary compile projects must not consume real interactive jobs.
            if (Application.isBatchMode) return;
            _nextCommandCheck = EditorApplication.timeSinceStartup + 1.0;
            _nextHeartbeat = 0.0;
            EditorApplication.update += Update;
            Application.logMessageReceived += OnLogMessage;
        }

        private static void Update()
        {
            double now = EditorApplication.timeSinceStartup;
            if (now >= _nextHeartbeat)
            {
                _nextHeartbeat = now + 5.0;
                WriteHeartbeat();
            }
            if (_active != null)
            {
                MonitorActiveDownload();
                return;
            }
            if (now < _nextCommandCheck || EditorApplication.isCompiling)
                return;
            _nextCommandCheck = now + 1.0;
            TryClaimCommand();
        }

        private static void TryClaimCommand()
        {
            if (!File.Exists(CommandPath))
                return;
            try
            {
                FileInfo info = new FileInfo(CommandPath);
                if (info.Length <= 0 || info.Length > MaxCommandBytes)
                    throw new InvalidDataException("Fafnir download command size is invalid.");
                DownloadCommand command = JsonUtility.FromJson<DownloadCommand>(
                    File.ReadAllText(CommandPath, Encoding.UTF8));
                ValidateCommand(command);
                ResolveDownloadManager();

                _active = command;
                _activeStartedUtc = DateTime.UtcNow;
                LastProducts.Clear();
                _authenticationFailed = false;
                WriteStatus("starting", "Unity Package Manager is starting the download.", null);
                object started = _download.Invoke(_downloadManager, new object[] { command.productIds });
                // A false return means Unity has started its asynchronous Terms of
                // Service check. The supplied callback starts the products after the
                // already-accepted account state is confirmed, so keep monitoring.
                if (started is bool && !(bool)started)
                    WriteStatus(
                        "starting",
                        "Unity Package Manager is checking the account download terms.",
                        null);
                File.Delete(CommandPath);
            }
            catch (Exception exception)
            {
                Exception actual = Unwrap(exception);
                DownloadCommand failed = _active ?? SafeReadCommand();
                _active = failed;
                WriteStatus("failed", actual.Message, null);
                _active = null;
                TryDeleteCommand();
                Debug.LogException(actual);
            }
        }

        private static void ValidateCommand(DownloadCommand command)
        {
            if (command == null || command.schema != CommandSchema)
                throw new InvalidDataException("Unsupported Fafnir download command schema.");
            if (string.IsNullOrEmpty(command.jobId) || command.jobId.Length > 100)
                throw new InvalidDataException("Fafnir download job ID is invalid.");
            DateTime expiresAtUtc;
            if (!DateTime.TryParse(
                    command.expiresAtUtc,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal,
                    out expiresAtUtc)
                || expiresAtUtc <= DateTime.UtcNow)
                throw new InvalidDataException("Fafnir download command has expired.");
            if (command.productIds == null
                || command.productIds.Length == 0
                || command.productIds.Length > MaxProducts
                || command.productIds.Any(value => value <= 0)
                || command.productIds.Distinct().Count() != command.productIds.Length)
                throw new InvalidDataException("Fafnir product ID list is invalid.");
        }

        private static void ResolveDownloadManager()
        {
            if (_downloadManager != null && _download != null && _getOperation != null)
                return;
            Assembly editorAssembly = typeof(UnityEditor.Editor).Assembly;
            Type containerType = RequireType(editorAssembly, "ServicesContainer");
            Type serviceType = RequireType(editorAssembly, "IAssetStoreDownloadManager");
            Type singletonType = typeof(ScriptableSingleton<>).MakeGenericType(containerType);
            PropertyInfo instanceProperty = singletonType.GetProperty(
                "instance", BindingFlags.Public | BindingFlags.Static);
            object container = instanceProperty == null
                ? null
                : instanceProperty.GetValue(null, null);
            if (container == null)
                throw new InvalidOperationException(
                    "Unity Package Manager service container is not initialized.");
            MethodInfo resolve = containerType
                .GetMethods(BindingFlags.Public | BindingFlags.Instance)
                .FirstOrDefault(method => method.Name == "Resolve" && method.IsGenericMethodDefinition);
            if (resolve == null)
                throw new MissingMethodException("Unity service resolver is unavailable.");
            _downloadManager = resolve.MakeGenericMethod(serviceType).Invoke(container, null);
            if (_downloadManager == null)
                throw new InvalidOperationException(
                    "Unity Asset Store download manager could not be resolved.");

            _download = serviceType
                .GetMethods(BindingFlags.Public | BindingFlags.Instance)
                .FirstOrDefault(method =>
                    method.Name == "Download"
                    && method.ReturnType == typeof(bool)
                    && method.GetParameters().Length == 1
                    && typeof(IEnumerable<long>).IsAssignableFrom(
                        method.GetParameters()[0].ParameterType));
            _getOperation = serviceType
                .GetMethods(BindingFlags.Public | BindingFlags.Instance)
                .FirstOrDefault(method =>
                    method.Name == "GetDownloadOperation"
                    && method.GetParameters().Length == 1);
            if (_download == null || _getOperation == null)
                throw new MissingMethodException(
                    "This Unity Editor version does not expose the expected Asset Store downloader.");
        }

        private static Type RequireType(Assembly assembly, string shortName)
        {
            Type value = assembly.GetType(InternalNamespace + "." + shortName, false);
            if (value == null)
                throw new TypeLoadException("Unity internal type is unavailable: " + shortName);
            return value;
        }

        private static void MonitorActiveDownload()
        {
            try
            {
                if (_authenticationFailed)
                {
                    WriteStatus("failed",
                        "Unity could not authenticate the Asset Store download. Restore the account "
                        + "session via Unity Hub/My Assets in this Editor, then retry. No project switch is needed.",
                        null, "unity_authentication_required");
                    _active = null;
                    return;
                }
                List<ProductStatus> products = new List<ProductStatus>();
                int completed = 0;
                bool failed = false;
                string failure = string.Empty;
                foreach (long productId in _active.productIds)
                {
                    object operation = _getOperation.Invoke(
                        _downloadManager, new object[] { productId });
                    ProductStatus status;
                    if (operation != null)
                    {
                        status = ReadProductStatus(productId, operation);
                        LastProducts[productId] = status;
                    }
                    else if (LastProducts.TryGetValue(productId, out status)
                        && !string.IsNullOrEmpty(status.packagePath)
                        && File.Exists(status.packagePath))
                    {
                        status.state = "completed";
                        status.progress = 1f;
                    }
                    else
                    {
                        status = new ProductStatus
                        {
                            productId = productId,
                            state = "waiting",
                            progress = 0f,
                            message = string.Empty,
                            packagePath = string.Empty
                        };
                    }
                    products.Add(status);
                    if (status.state == "completed")
                        completed++;
                    else if (status.state == "failed" || status.state == "aborted")
                    {
                        failed = true;
                        if (string.IsNullOrEmpty(failure))
                            failure = status.message;
                    }
                }

                if (failed)
                {
                    WriteStatus("failed", failure, products);
                    _active = null;
                }
                else if (completed == _active.productIds.Length)
                {
                    WriteStatus("completed", "All requested Asset Store packages were downloaded.", products);
                    _active = null;
                }
                else if (products.All(item => item.state == "waiting")
                    && DateTime.UtcNow - _activeStartedUtc > TimeSpan.FromSeconds(120))
                {
                    WriteStatus(
                        "failed",
                        "Unity did not expose download operations within 120 seconds.",
                        products);
                    _active = null;
                }
                else
                {
                    bool waiting = products.All(item => item.state == "waiting");
                    WriteStatus(waiting ? "starting" : "downloading",
                        waiting ? "Waiting for Unity to start the download; no package transfer is confirmed yet."
                            : "Unity Package Manager is downloading packages.", products);
                }
            }
            catch (Exception exception)
            {
                Exception actual = Unwrap(exception);
                WriteStatus("failed", actual.Message, null);
                _active = null;
                Debug.LogException(actual);
            }
        }

        private static ProductStatus ReadProductStatus(long productId, object operation)
        {
            ProductStatus result = new ProductStatus
            {
                productId = productId,
                state = "waiting",
                progress = 0f,
                message = string.Empty,
                packagePath = string.Empty
            };
            if (operation == null)
                return result;
            object rawState = ReadMember(operation, "state");
            int state = rawState == null
                ? 0
                : Convert.ToInt32(rawState, CultureInfo.InvariantCulture);
            object progress = ReadMember(operation, "progressPercentage");
            if (progress != null)
                result.progress = Mathf.Clamp01(
                    Convert.ToSingle(progress, CultureInfo.InvariantCulture));
            result.message = Convert.ToString(
                ReadMember(operation, "errorMessage"), CultureInfo.InvariantCulture) ?? string.Empty;
            result.packagePath = Convert.ToString(
                ReadMember(operation, "packageNewPath"), CultureInfo.InvariantCulture) ?? string.Empty;
            if ((state & ErrorState) != 0)
                result.state = "failed";
            else if ((state & AbortedState) != 0)
                result.state = "aborted";
            else if ((state & CompletedState) != 0)
            {
                result.state = "completed";
                result.progress = 1f;
            }
            else
                result.state = rawState == null
                    ? "waiting"
                    : rawState.ToString().ToLowerInvariant();
            return result;
        }

        private static object ReadMember(object target, string name)
        {
            if (target == null)
                return null;
            Type type = target.GetType();
            PropertyInfo property = type.GetProperty(
                name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (property != null)
                return property.GetValue(target, null);
            FieldInfo field = type.GetField(
                name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            return field == null ? null : field.GetValue(target);
        }

        private static void WriteStatus(
            string state,
            string message,
            IEnumerable<ProductStatus> products,
            string errorCode = "")
        {
            DownloadStatus payload = new DownloadStatus
            {
                schema = StatusSchema,
                jobId = _active == null ? string.Empty : _active.jobId,
                state = state,
                updatedAtUtc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                message = message ?? string.Empty,
                errorCode = errorCode,
                products = products == null ? new List<ProductStatus>() : products.ToList()
            };
            try
            {
                WriteJson(StatusPath, JsonUtility.ToJson(payload, true));
            }
            catch (IOException exception)
            {
                // Status readers must never be able to abort the Unity download.
                // The next Editor update will publish a fresh snapshot.
                Debug.LogWarning("Fafnir download status write deferred: " + exception.Message);
            }
        }

        private static void WriteHeartbeat()
        {
            BridgeStatus payload = new BridgeStatus
            {
                schema = BridgeSchema,
                updatedAtUtc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                unityVersion = Application.unityVersion,
                projectName = Application.productName,
                accountState = ReadAccountState(),
                busy = EditorApplication.isCompiling || _active != null || FafnirAssetStoreImportBridge.IsBusy,
                projectPath = Path.GetFullPath(Path.Combine(Application.dataPath, "..")),
                supportsImportDialog = true,
                bridgeVersion = FafnirBridgeDiagnostics.Version,
                bridgeContentHash = FafnirBridgeDiagnostics.ContentHash,
                compilationState = FafnirBridgeDiagnostics.CompilationState,
                syncState = FafnirBridgeDiagnostics.SyncState,
                syncCount = FafnirBridgeDiagnostics.SyncCount,
                syncSuccessAtUtc = FafnirBridgeDiagnostics.SyncTime
            };
            try
            {
                WriteJson(BridgePath, JsonUtility.ToJson(payload, true));
            }
            catch (Exception exception)
            {
                Debug.LogWarning("Fafnir download bridge heartbeat failed: " + exception.Message);
            }
        }

        private static string ReadAccountState()
        {
            // Read only the signed-in boolean, never identifiers or credentials.
            try
            {
                Type type = typeof(UnityEditor.Editor).Assembly.GetType("UnityEditor.Connect.UnityConnect");
                PropertyInfo instance = type == null ? null : type.GetProperty(
                    "instance", BindingFlags.Public | BindingFlags.Static);
                object connect = instance == null ? null : instance.GetValue(null, null);
                object loggedIn = ReadMember(connect, "loggedIn");
                return loggedIn is bool ? ((bool)loggedIn ? "signed_in" : "signed_out") : "unknown";
            }
            catch
            {
                // An unavailable probe is not evidence that the user is signed out.
                return "unknown";
            }
        }

        private static void OnLogMessage(string message, string stackTrace, LogType type)
        {
            // This error can precede creation of a download operation. Retain only a
            // fixed classification, never the log text or any session values.
            if (_active != null && (type == LogType.Error || type == LogType.Exception)
                && message != null
                && message.IndexOf("Error while getting access token", StringComparison.OrdinalIgnoreCase) >= 0
                && message.IndexOf("invalid configuration from Unity Connect", StringComparison.OrdinalIgnoreCase) >= 0)
                _authenticationFailed = true;
        }

        private static void WriteJson(string path, string json)
        {
            Directory.CreateDirectory(RootPath);
            string temporary = path + ".tmp";
            for (int attempt = 0; attempt < JsonWriteAttempts; attempt++)
            {
                try
                {
                    File.WriteAllText(temporary, json, new UTF8Encoding(false));
                    if (File.Exists(path))
                    {
                        try
                        {
                            File.Replace(temporary, path, null);
                            return;
                        }
                        catch (PlatformNotSupportedException)
                        {
                            File.Delete(path);
                        }
                    }
                    File.Move(temporary, path);
                    return;
                }
                catch (IOException) when (attempt + 1 < JsonWriteAttempts)
                {
                    try
                    {
                        if (File.Exists(temporary))
                            File.Delete(temporary);
                    }
                    catch (IOException)
                    {
                        // A later retry will attempt the same cleanup again.
                    }
                    System.Threading.Thread.Sleep(25 * (attempt + 1));
                }
            }
        }

        private static DownloadCommand SafeReadCommand()
        {
            try
            {
                return JsonUtility.FromJson<DownloadCommand>(
                    File.ReadAllText(CommandPath, Encoding.UTF8));
            }
            catch
            {
                return null;
            }
        }

        private static void TryDeleteCommand()
        {
            try
            {
                if (File.Exists(CommandPath))
                    File.Delete(CommandPath);
            }
            catch (IOException)
            {
                // The next poll can retry cleanup after another process releases the file.
            }
        }

        private static Exception Unwrap(Exception exception)
        {
            TargetInvocationException invocation = exception as TargetInvocationException;
            return invocation != null && invocation.InnerException != null
                ? invocation.InnerException
                : exception;
        }

        [Serializable]
        private sealed class DownloadCommand
        {
            public string schema;
            public string jobId;
            public string createdAtUtc;
            public string expiresAtUtc;
            public long[] productIds;
        }

        [Serializable]
        private sealed class DownloadStatus
        {
            public string schema;
            public string jobId;
            public string state;
            public string updatedAtUtc;
            public string message;
            public string errorCode;
            public List<ProductStatus> products;
        }

        [Serializable]
        private sealed class ProductStatus
        {
            public long productId;
            public string state;
            public float progress;
            public string message;
            public string packagePath;
        }

        [Serializable]
        private sealed class BridgeStatus
        {
            public string schema;
            public string updatedAtUtc;
            public string unityVersion;
            public string projectName;
            public string accountState;
            public bool busy;
            public string projectPath;
            public bool supportsImportDialog;
            public string bridgeVersion;
            public string bridgeContentHash;
            public string compilationState;
            public string syncState;
            public int syncCount;
            public string syncSuccessAtUtc;
        }
    }
}

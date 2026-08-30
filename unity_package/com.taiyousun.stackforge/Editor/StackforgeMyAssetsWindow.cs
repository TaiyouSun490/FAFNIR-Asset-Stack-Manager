// SPDX-License-Identifier: MIT
// This bridge uses runtime reflection so it does not redistribute Unity's
// reference-only Package Manager source or expose the Editor OAuth token.

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
    internal sealed class StackforgeMyAssetsWindow : EditorWindow
    {
        [MenuItem("Tools/Stackforge/My Assets Sync")]
        private static void Open()
        {
            GetWindow<StackforgeMyAssetsWindow>("Stackforge My Assets");
        }

        private void OnEnable()
        {
            StackforgeMyAssetsSync.changed += Repaint;
        }

        private void OnDisable()
        {
            StackforgeMyAssetsSync.changed -= Repaint;
        }

        private void OnGUI()
        {
            EditorGUILayout.LabelField("Stackforge My Assets", EditorStyles.boldLabel);
            EditorGUILayout.HelpBox(
                "Unity Editorでログイン中のMy Assetsから、商品ID・商品名・タグ・購入日時だけを取得します。認証トークン、商品本文、画像、アセット本体は出力しません。",
                MessageType.Info);
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("出力先", StackforgeMyAssetsSync.exportPath);
            EditorGUILayout.Space();

            using (new EditorGUI.DisabledScope(StackforgeMyAssetsSync.isRunning))
            {
                if (GUILayout.Button("My Assetsを同期", GUILayout.Height(34)))
                    StackforgeMyAssetsSync.Start();
            }

            EditorGUILayout.Space();
            EditorGUILayout.HelpBox(StackforgeMyAssetsSync.status, StackforgeMyAssetsSync.messageType);
            if (StackforgeMyAssetsSync.lastCount >= 0)
                EditorGUILayout.LabelField("取得件数", StackforgeMyAssetsSync.lastCount.ToString(CultureInfo.InvariantCulture));

            if (GUILayout.Button("Stackforgeを開く"))
                Application.OpenURL("http://127.0.0.1:8770/");
        }
    }

    internal static class StackforgeMyAssetsSync
    {
        private const string InternalNamespace = "UnityEditor.PackageManager.UI.Internal";
        private const int PageSize = 500;
        private const int MaxAssets = 20000;

        internal static event Action changed = delegate { };
        internal static bool isRunning { get; private set; }
        internal static string status { get; private set; } = "同期待ちです。";
        internal static MessageType messageType { get; private set; } = MessageType.None;
        internal static int lastCount { get; private set; } = -1;

        internal static string exportPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "game-stack-planner",
                    "unity-my-assets.json");
            }
        }

        private static readonly Dictionary<long, OwnedAssetRecord> Records =
            new Dictionary<long, OwnedAssetRecord>();
        private static Assembly _editorAssembly;
        private static object _restApi;
        private static Type _queryType;
        private static MethodInfo _getPurchases;
        private static int _offset;
        private static bool _hiddenPass;

        internal static void Start()
        {
            if (isRunning)
                return;
            isRunning = true;
            lastCount = -1;
            status = "Unity Package Managerへ接続中…";
            messageType = MessageType.Info;
            Records.Clear();
            _offset = 0;
            _hiddenPass = false;
            Notify();

            try
            {
                ResolveApi();
                RequestPage();
            }
            catch (Exception exception)
            {
                Fail(exception);
            }
        }

        private static void ResolveApi()
        {
            _editorAssembly = typeof(UnityEditor.Editor).Assembly;
            Type containerType = RequireType("ServicesContainer");
            Type serviceType = RequireType("IAssetStoreRestAPI");
            _queryType = RequireType("PurchasesQueryArgs");

            Type singletonType = typeof(ScriptableSingleton<>).MakeGenericType(containerType);
            PropertyInfo instanceProperty = singletonType.GetProperty(
                "instance",
                BindingFlags.Public | BindingFlags.Static);
            if (instanceProperty == null)
                throw new MissingMemberException("Unity Package Manager service container is unavailable.");
            object container = instanceProperty.GetValue(null, null);
            if (container == null)
                throw new InvalidOperationException("Unity Package Manager service container is not initialized.");

            MethodInfo resolve = containerType.GetMethods(BindingFlags.Public | BindingFlags.Instance)
                .FirstOrDefault(method => method.Name == "Resolve" && method.IsGenericMethodDefinition);
            if (resolve == null)
                throw new MissingMethodException("Unity Package Manager service resolver is unavailable.");
            _restApi = resolve.MakeGenericMethod(serviceType).Invoke(container, null);
            if (_restApi == null)
                throw new InvalidOperationException("Unity Asset Store service could not be resolved.");

            _getPurchases = _restApi.GetType()
                .GetMethods(BindingFlags.Public | BindingFlags.Instance)
                .FirstOrDefault(method => method.Name == "GetPurchases" && method.GetParameters().Length == 3);
            if (_getPurchases == null)
                throw new MissingMethodException("Unity My Assets API is unavailable in this Editor version.");
        }

        private static Type RequireType(string shortName)
        {
            Type value = _editorAssembly.GetType(InternalNamespace + "." + shortName, false);
            if (value == null)
                throw new TypeLoadException("Unity internal type is unavailable: " + shortName);
            return value;
        }

        private static void RequestPage()
        {
            status = _hiddenPass
                ? "非表示のMy Assetsを取得中…"
                : "My Assetsを取得中… " + Records.Count.ToString(CultureInfo.InvariantCulture) + "件";
            Notify();

            object query = Activator.CreateInstance(
                _queryType,
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance,
                null,
                new object[] { _offset, PageSize, string.Empty, null },
                CultureInfo.InvariantCulture);
            if (query == null)
                throw new InvalidOperationException("Unity My Assets query could not be created.");
            if (_hiddenPass)
                SetHiddenFilter(query);

            ParameterInfo[] parameters = _getPurchases.GetParameters();
            Delegate success = CreateCallback(parameters[1].ParameterType, "OnPage");
            Delegate failure = CreateCallback(parameters[2].ParameterType, "OnError");
            _getPurchases.Invoke(_restApi, new object[] { query, success, failure });
        }

        private static void SetHiddenFilter(object query)
        {
            Type statusType = RequireType("PageFilterStatus");
            object hidden = Enum.Parse(statusType, "Hidden");
            MethodInfo update = _queryType.GetMethod(
                "UpdateStatus",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (update == null)
                throw new MissingMethodException("Unity hidden-assets filter is unavailable.");
            update.Invoke(query, new[] { hidden });
        }

        private static Delegate CreateCallback(Type delegateType, string methodName)
        {
            Type argumentType = delegateType.GetGenericArguments()[0];
            MethodInfo method = typeof(StackforgeMyAssetsSync).GetMethod(
                methodName,
                BindingFlags.NonPublic | BindingFlags.Static);
            if (method == null)
                throw new MissingMethodException(methodName);
            return Delegate.CreateDelegate(delegateType, method.MakeGenericMethod(argumentType));
        }

        private static void OnPage<T>(T page)
        {
            try
            {
                long total = Convert.ToInt64(ReadMember(page, "total"), CultureInfo.InvariantCulture);
                IEnumerable items = ReadMember(page, "list") as IEnumerable;
                int pageCount = 0;
                if (items != null)
                {
                    foreach (object item in items)
                    {
                        if (item == null)
                            continue;
                        OwnedAssetRecord record = ReadAsset(item);
                        Records[record.productId] = record;
                        pageCount++;
                        if (Records.Count > MaxAssets)
                            throw new InvalidDataException("My Assets count exceeds the Stackforge safety limit.");
                    }
                }

                if (pageCount > 0 && _offset + pageCount < total)
                {
                    _offset += pageCount;
                    RequestPage();
                    return;
                }
                if (!_hiddenPass)
                {
                    _hiddenPass = true;
                    _offset = 0;
                    RequestPage();
                    return;
                }
                WriteExport();
            }
            catch (Exception exception)
            {
                Fail(exception);
            }
        }

        private static void OnError<T>(T error)
        {
            object detail = ReadMember(error, "message");
            string message = detail == null ? "Unity My Assets request failed." : detail.ToString();
            Fail(new InvalidOperationException(message));
        }

        private static OwnedAssetRecord ReadAsset(object item)
        {
            long productId = Convert.ToInt64(ReadMember(item, "productId"), CultureInfo.InvariantCulture);
            string displayName = Convert.ToString(ReadMember(item, "displayName"), CultureInfo.InvariantCulture);
            string purchasedTime = Convert.ToString(ReadMember(item, "purchasedTime"), CultureInfo.InvariantCulture);
            bool hidden = Convert.ToBoolean(ReadMember(item, "isHidden"), CultureInfo.InvariantCulture);
            object rawTags = ReadMember(item, "tags");
            string[] tags = rawTags is IEnumerable values
                ? values.Cast<object>().Where(value => value != null).Select(value => value.ToString()).ToArray()
                : Array.Empty<string>();
            return new OwnedAssetRecord
            {
                productId = productId,
                displayName = string.IsNullOrWhiteSpace(displayName)
                    ? "Unity Asset Store product " + productId.ToString(CultureInfo.InvariantCulture)
                    : displayName,
                purchasedTime = purchasedTime ?? string.Empty,
                tags = tags,
                hidden = hidden
            };
        }

        private static object ReadMember(object target, string name)
        {
            if (target == null)
                return null;
            Type type = target.GetType();
            FieldInfo field = type.GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (field != null)
                return field.GetValue(target);
            PropertyInfo property = type.GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            return property == null ? null : property.GetValue(target, null);
        }

        private static void WriteExport()
        {
            OwnedAssetsExport payload = new OwnedAssetsExport
            {
                schema = "stackforge.unity-my-assets.v1",
                generatedAtUtc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                unityVersion = Application.unityVersion,
                assets = Records.Values.OrderBy(asset => asset.displayName, StringComparer.OrdinalIgnoreCase).ToList()
            };
            string directory = Path.GetDirectoryName(exportPath);
            Directory.CreateDirectory(directory);
            string temporary = exportPath + ".tmp";
            File.WriteAllText(temporary, JsonUtility.ToJson(payload, true), new UTF8Encoding(false));
            if (File.Exists(exportPath))
            {
                try
                {
                    File.Replace(temporary, exportPath, null);
                }
                catch (PlatformNotSupportedException)
                {
                    File.Delete(exportPath);
                    File.Move(temporary, exportPath);
                }
            }
            else
            {
                File.Move(temporary, exportPath);
            }
            isRunning = false;
            lastCount = Records.Count;
            status = "同期ファイルを書き出しました。Stackforgeで「所有アセットを同期」を押してください。";
            messageType = MessageType.Info;
            Notify();
        }

        private static void Fail(Exception exception)
        {
            TargetInvocationException invocation = exception as TargetInvocationException;
            Exception actual = invocation != null && invocation.InnerException != null
                ? invocation.InnerException
                : exception;
            isRunning = false;
            status = actual.Message.IndexOf("not logged in", StringComparison.OrdinalIgnoreCase) >= 0
                ? "Unity Hub / Unity Editorへサインインしてから、もう一度同期してください。"
                : actual.Message;
            messageType = MessageType.Error;
            Debug.LogException(actual);
            Notify();
        }

        private static void Notify()
        {
            changed();
        }

        [Serializable]
        private sealed class OwnedAssetsExport
        {
            public string schema;
            public string generatedAtUtc;
            public string unityVersion;
            public List<OwnedAssetRecord> assets;
        }

        [Serializable]
        private sealed class OwnedAssetRecord
        {
            public long productId;
            public string displayName;
            public string purchasedTime;
            public string[] tags;
            public bool hidden;
        }
    }
}

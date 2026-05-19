#if UNITY_EDITOR
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using UnityEditor;
using UnityEngine;

public class MaterialFitUnityMultiViewCapture : EditorWindow
{
    private GameObject targetObject;
    private Camera captureCamera;
    private DefaultAsset outputFolder;
    private string outputFolderPath = "Assets/MaterialFitCaptures";
    private string filePrefix = "unity_ref";
    private string yawDegrees = "0,45,90,135,180,225,270,315";
    private string pitchDegrees = "0";
    private int imageWidth = 900;
    private int imageHeight = 600;
    private float distanceScale = 2.2f;
    private float minDistance = 1.0f;
    private float fieldOfView = 35.0f;
    private Color backgroundColor = Color.clear;
    private bool transparentBackground = true;
    private bool useSilhouetteMaskAlpha = true;
    private bool keyBackgroundToAlpha = false;
    private bool exportMask = true;
    private float backgroundKeyTolerance = 0.02f;
    private float backgroundKeySoftness = 0.06f;
    private bool useCameraProjection = true;
    private bool useOrthographic = false;
    private float orthographicScale = 1.2f;

    [MenuItem("Material Fit/Multi-view Capture Window", false, 20)]
    public static void ShowWindow()
    {
        GetWindow<MaterialFitUnityMultiViewCapture>("Material Fit Capture");
    }

    private void OnEnable()
    {
        if (targetObject == null)
        {
            targetObject = Selection.activeGameObject;
        }
        if (captureCamera == null)
        {
            captureCamera = Camera.main;
        }
    }

    private void OnGUI()
    {
        EditorGUILayout.LabelField("Reference Capture", EditorStyles.boldLabel);
        targetObject = (GameObject)EditorGUILayout.ObjectField("Target Object", targetObject, typeof(GameObject), true);
        captureCamera = (Camera)EditorGUILayout.ObjectField("Camera", captureCamera, typeof(Camera), true);
        outputFolder = (DefaultAsset)EditorGUILayout.ObjectField("Output Folder", outputFolder, typeof(DefaultAsset), false);
        if (outputFolder != null)
        {
            string assetPath = AssetDatabase.GetAssetPath(outputFolder);
            if (AssetDatabase.IsValidFolder(assetPath))
            {
                outputFolderPath = assetPath;
            }
        }
        outputFolderPath = EditorGUILayout.TextField("Output Path", outputFolderPath);
        filePrefix = EditorGUILayout.TextField("File Prefix", filePrefix);

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("Views", EditorStyles.boldLabel);
        yawDegrees = EditorGUILayout.TextField("Yaw Degrees", yawDegrees);
        pitchDegrees = EditorGUILayout.TextField("Pitch Degrees", pitchDegrees);
        distanceScale = EditorGUILayout.FloatField("Distance Scale", distanceScale);
        minDistance = EditorGUILayout.FloatField("Min Distance", minDistance);

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("Render", EditorStyles.boldLabel);
        imageWidth = EditorGUILayout.IntField("Width", imageWidth);
        imageHeight = EditorGUILayout.IntField("Height", imageHeight);
        useCameraProjection = EditorGUILayout.Toggle("Use Camera Projection", useCameraProjection);
        if (useCameraProjection && captureCamera != null)
        {
            using (new EditorGUI.DisabledScope(true))
            {
                EditorGUILayout.Toggle("Camera Orthographic", captureCamera.orthographic);
                if (captureCamera.orthographic)
                {
                    EditorGUILayout.FloatField("Camera Ortho Size", captureCamera.orthographicSize);
                }
                else
                {
                    EditorGUILayout.FloatField("Camera Field Of View", captureCamera.fieldOfView);
                }
            }
        }
        else
        {
            useOrthographic = EditorGUILayout.Toggle("Orthographic", useOrthographic);
            if (useOrthographic)
            {
                orthographicScale = EditorGUILayout.FloatField("Ortho Scale", orthographicScale);
            }
            else
            {
                fieldOfView = EditorGUILayout.FloatField("Field Of View", fieldOfView);
            }
        }
        transparentBackground = EditorGUILayout.Toggle("Transparent BG", transparentBackground);
        backgroundColor = EditorGUILayout.ColorField("Background", backgroundColor);
        useSilhouetteMaskAlpha = EditorGUILayout.Toggle("Silhouette Mask Alpha", useSilhouetteMaskAlpha);
        keyBackgroundToAlpha = EditorGUILayout.Toggle("Key BG To Alpha", keyBackgroundToAlpha);
        using (new EditorGUI.DisabledScope(!keyBackgroundToAlpha || useSilhouetteMaskAlpha))
        {
            backgroundKeyTolerance = EditorGUILayout.Slider("Key Tolerance", backgroundKeyTolerance, 0.0f, 0.25f);
            backgroundKeySoftness = EditorGUILayout.Slider("Key Softness", backgroundKeySoftness, 0.0f, 0.25f);
        }
        exportMask = EditorGUILayout.Toggle("Export Alpha Mask", exportMask);

        EditorGUILayout.Space();
        using (new EditorGUI.DisabledScope(targetObject == null || imageWidth <= 0 || imageHeight <= 0))
        {
            if (GUILayout.Button("Capture Multi-view References"))
            {
                Capture();
            }
        }

        EditorGUILayout.HelpBox(
            "Place this file in a Unity Editor folder. Select the fish/model root, choose an output folder, then export one PNG per yaw/pitch view plus metadata JSON.",
            MessageType.Info);
    }

    private void Capture()
    {
        List<float> yaws = ParseFloatList(yawDegrees);
        List<float> pitches = ParseFloatList(pitchDegrees);
        if (yaws.Count == 0 || pitches.Count == 0)
        {
            EditorUtility.DisplayDialog("Material Fit", "Yaw Degrees and Pitch Degrees must contain at least one number.", "OK");
            return;
        }

        Bounds bounds;
        if (!TryGetRenderBounds(targetObject, out bounds))
        {
            EditorUtility.DisplayDialog("Material Fit", "Target Object has no Renderer bounds.", "OK");
            return;
        }

        string absoluteOutputPath = ResolveOutputPath(outputFolderPath);
        Directory.CreateDirectory(absoluteOutputPath);

        Camera camera = captureCamera;
        GameObject temporaryCameraObject = null;
        if (camera == null)
        {
            temporaryCameraObject = new GameObject("MaterialFit_TemporaryCaptureCamera");
            temporaryCameraObject.hideFlags = HideFlags.HideAndDontSave;
            camera = temporaryCameraObject.AddComponent<Camera>();
        }

        CameraState originalState = CameraState.FromCamera(camera);
        TransformState originalTargetState = TransformState.FromTransform(targetObject.transform);
        bool rotateTargetInsteadOfCamera = targetObject != null && camera != null && IsDescendantOf(targetObject.transform, camera.transform);
        bool effectiveUseOrthographic = useCameraProjection && captureCamera != null ? camera.orthographic : useOrthographic;
        float effectiveFieldOfView = useCameraProjection && captureCamera != null ? camera.fieldOfView : fieldOfView;
        float effectiveOrthographicSize = useCameraProjection && captureCamera != null
            ? camera.orthographicSize
            : Mathf.Max(0.01f, bounds.extents.magnitude * orthographicScale);
        CaptureMetadata metadata = new CaptureMetadata
        {
            exporterVersion = "1.0.0",
            exportedAtUtc = System.DateTime.UtcNow.ToString("o"),
            unityVersion = Application.unityVersion,
            targetName = targetObject.name,
            targetAssetPath = GetTargetAssetPath(targetObject),
            outputFolder = absoluteOutputPath,
            imageWidth = imageWidth,
            imageHeight = imageHeight,
            transparentBackground = transparentBackground,
            useSilhouetteMaskAlpha = useSilhouetteMaskAlpha,
            keyBackgroundToAlpha = keyBackgroundToAlpha,
            exportMask = exportMask,
            backgroundKeyTolerance = backgroundKeyTolerance,
            backgroundKeySoftness = backgroundKeySoftness,
            useCameraProjection = useCameraProjection,
            captureMode = rotateTargetInsteadOfCamera ? "rotate_target" : "orbit_camera",
            useOrthographic = effectiveUseOrthographic,
            fieldOfView = effectiveFieldOfView,
            orthographicScale = orthographicScale,
            orthographicSize = effectiveOrthographicSize,
            targetCenter = ToArray(bounds.center),
            targetSize = ToArray(bounds.size)
        };

        try
        {
            int viewIndex = 0;
            foreach (float pitch in pitches)
            {
                foreach (float yaw in yaws)
                {
                    string fileName = string.Format(CultureInfo.InvariantCulture, "{0}_v{1:000}_yaw{2}_pitch{3}.png", filePrefix, viewIndex, FormatAngle(yaw), FormatAngle(pitch));
                    string maskFileName = string.Format(CultureInfo.InvariantCulture, "{0}_v{1:000}_yaw{2}_pitch{3}_mask.png", filePrefix, viewIndex, FormatAngle(yaw), FormatAngle(pitch));
                    string imagePath = Path.Combine(absoluteOutputPath, fileName);
                    string maskPath = exportMask ? Path.Combine(absoluteOutputPath, maskFileName) : string.Empty;
                    if (rotateTargetInsteadOfCamera)
                    {
                        ConfigureFixedCameraForCapture(camera, bounds, effectiveUseOrthographic, effectiveFieldOfView, effectiveOrthographicSize);
                        RotateTargetForView(targetObject.transform, originalTargetState.localRotation, yaw, pitch);
                    }
                    else
                    {
                        ConfigureCameraForView(camera, bounds, yaw, pitch, effectiveUseOrthographic, effectiveFieldOfView, effectiveOrthographicSize);
                    }
                    RenderCameraToPng(camera, targetObject, imagePath, maskPath, imageWidth, imageHeight, backgroundColor, useSilhouetteMaskAlpha, keyBackgroundToAlpha, backgroundKeyTolerance, backgroundKeySoftness);
                    metadata.views.Add(new CaptureView
                    {
                        index = viewIndex,
                        yaw = yaw,
                        pitch = pitch,
                        imagePath = imagePath,
                        fileName = fileName,
                        maskPath = maskPath,
                        maskFileName = exportMask ? maskFileName : string.Empty,
                        cameraPosition = ToArray(camera.transform.position),
                        cameraRotationEuler = ToArray(camera.transform.rotation.eulerAngles),
                        targetLocalRotationEuler = ToArray(targetObject.transform.localRotation.eulerAngles)
                    });
                    viewIndex++;
                }
            }

            string metadataPath = Path.Combine(absoluteOutputPath, filePrefix + "_multiview_metadata.json");
            File.WriteAllText(metadataPath, UnityEngine.JsonUtility.ToJson(metadata, true));
            AssetDatabase.Refresh();
            Debug.Log("Material Fit multi-view capture exported: " + absoluteOutputPath);
        }
        finally
        {
            originalTargetState.ApplyTo(targetObject.transform);
            originalState.ApplyTo(camera);
            if (temporaryCameraObject != null)
            {
                DestroyImmediate(temporaryCameraObject);
            }
        }
    }

    private void ConfigureCameraForView(Camera camera, Bounds bounds, float yaw, float pitch, bool effectiveUseOrthographic, float effectiveFieldOfView, float effectiveOrthographicSize)
    {
        Vector3 center = bounds.center;
        float radius = bounds.extents.magnitude;
        float distance = Mathf.Max(minDistance, radius * distanceScale);
        Quaternion viewRotation = Quaternion.Euler(pitch, yaw, 0.0f);
        Vector3 forward = viewRotation * Vector3.forward;

        camera.transform.position = center - forward * distance;
        camera.transform.rotation = Quaternion.LookRotation(forward, Vector3.up);
        camera.nearClipPlane = Mathf.Max(0.01f, distance - radius * 3.0f);
        camera.farClipPlane = distance + radius * 4.0f;
        ApplyProjectionAndClear(camera, effectiveUseOrthographic, effectiveFieldOfView, effectiveOrthographicSize);
    }

    private void ConfigureFixedCameraForCapture(Camera camera, Bounds bounds, bool effectiveUseOrthographic, float effectiveFieldOfView, float effectiveOrthographicSize)
    {
        float radius = bounds.extents.magnitude;
        camera.nearClipPlane = 0.01f;
        camera.farClipPlane = Mathf.Max(camera.farClipPlane, radius * 10.0f + 100.0f);
        ApplyProjectionAndClear(camera, effectiveUseOrthographic, effectiveFieldOfView, effectiveOrthographicSize);
    }

    private void ApplyProjectionAndClear(Camera camera, bool effectiveUseOrthographic, float effectiveFieldOfView, float effectiveOrthographicSize)
    {
        camera.clearFlags = transparentBackground ? CameraClearFlags.SolidColor : CameraClearFlags.Skybox;
        camera.backgroundColor = backgroundColor;
        camera.orthographic = effectiveUseOrthographic;
        if (effectiveUseOrthographic)
        {
            camera.orthographicSize = Mathf.Max(0.01f, effectiveOrthographicSize);
        }
        else
        {
            camera.fieldOfView = effectiveFieldOfView;
        }
    }

    private static void RotateTargetForView(Transform target, Quaternion baseLocalRotation, float yaw, float pitch)
    {
        target.localRotation = baseLocalRotation * Quaternion.Euler(-pitch, -yaw, 0.0f);
    }

    private static void RenderCameraToPng(Camera camera, GameObject targetObject, string imagePath, string maskPath, int width, int height, Color keyColor, bool useSilhouetteMask, bool keyBackground, float keyTolerance, float keySoftness)
    {
        RenderTexture renderTexture = new RenderTexture(Mathf.Max(1, width), Mathf.Max(1, height), 24, RenderTextureFormat.ARGB32);
        renderTexture.antiAliasing = 8;
        RenderTexture previousActive = RenderTexture.active;
        RenderTexture previousTarget = camera.targetTexture;

        Texture2D texture = null;
        Texture2D maskTexture = null;
        try
        {
            camera.targetTexture = renderTexture;
            RenderTexture.active = renderTexture;
            camera.Render();

            texture = new Texture2D(renderTexture.width, renderTexture.height, TextureFormat.RGBA32, false);
            texture.ReadPixels(new Rect(0, 0, renderTexture.width, renderTexture.height), 0, 0);
            texture.Apply();

            if (useSilhouetteMask)
            {
                maskTexture = RenderSilhouetteMask(camera, targetObject, width, height);
                if (maskTexture != null)
                {
                    ApplyAlphaMask(texture, maskTexture);
                }
            }
            else if (keyBackground)
            {
                ApplyBackgroundAlphaKey(texture, keyColor, keyTolerance, keySoftness);
            }

            if (!string.IsNullOrEmpty(maskPath))
            {
                if (maskTexture != null)
                {
                    File.WriteAllBytes(maskPath, maskTexture.EncodeToPNG());
                }
                else
                {
                    WriteAlphaMaskPng(texture, maskPath);
                }
            }

            byte[] bytes = texture.EncodeToPNG();
            File.WriteAllBytes(imagePath, bytes);
        }
        finally
        {
            if (texture != null)
            {
                DestroyImmediate(texture);
            }
            if (maskTexture != null)
            {
                DestroyImmediate(maskTexture);
            }
            camera.targetTexture = previousTarget;
            RenderTexture.active = previousActive;
            renderTexture.Release();
            DestroyImmediate(renderTexture);
        }
    }

    private static Texture2D RenderSilhouetteMask(Camera camera, GameObject targetObject, int width, int height)
    {
        RenderTexture maskRenderTexture = new RenderTexture(Mathf.Max(1, width), Mathf.Max(1, height), 24, RenderTextureFormat.ARGB32);
        maskRenderTexture.antiAliasing = 8;
        RenderTexture previousActive = RenderTexture.active;
        RenderTexture previousTarget = camera.targetTexture;
        CameraClearFlags previousClearFlags = camera.clearFlags;
        Color previousBackground = camera.backgroundColor;

        Renderer[] targetRenderers = targetObject.GetComponentsInChildren<Renderer>(true);
        Renderer[] sceneRenderers = Object.FindObjectsOfType<Renderer>();
        List<RendererState> rendererStates = new List<RendererState>();
        Material maskMaterial = CreateMaskMaterial();
        if (maskMaterial == null)
        {
            return null;
        }

        try
        {
            HashSet<Renderer> targetSet = new HashSet<Renderer>(targetRenderers);
            for (int i = 0; i < sceneRenderers.Length; i++)
            {
                Renderer renderer = sceneRenderers[i];
                RendererState state = RendererState.FromRenderer(renderer);
                rendererStates.Add(state);
                if (targetSet.Contains(renderer))
                {
                    Material[] maskMaterials = new Material[renderer.sharedMaterials.Length];
                    for (int j = 0; j < maskMaterials.Length; j++)
                    {
                        maskMaterials[j] = maskMaterial;
                    }
                    renderer.sharedMaterials = maskMaterials;
                    renderer.enabled = true;
                }
                else
                {
                    renderer.enabled = false;
                }
            }

            camera.targetTexture = maskRenderTexture;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = Color.black;
            RenderTexture.active = maskRenderTexture;
            camera.Render();

            Texture2D mask = new Texture2D(maskRenderTexture.width, maskRenderTexture.height, TextureFormat.RGBA32, false);
            mask.ReadPixels(new Rect(0, 0, maskRenderTexture.width, maskRenderTexture.height), 0, 0);
            mask.Apply();
            NormalizeMaskTexture(mask);
            return mask;
        }
        finally
        {
            for (int i = 0; i < rendererStates.Count; i++)
            {
                rendererStates[i].Apply();
            }
            if (maskMaterial != null)
            {
                DestroyImmediate(maskMaterial);
            }
            camera.targetTexture = previousTarget;
            camera.clearFlags = previousClearFlags;
            camera.backgroundColor = previousBackground;
            RenderTexture.active = previousActive;
            maskRenderTexture.Release();
            DestroyImmediate(maskRenderTexture);
        }
    }

    private static Material CreateMaskMaterial()
    {
        Shader shader = Shader.Find("Unlit/Color");
        if (shader == null)
        {
            shader = Shader.Find("Hidden/Internal-Colored");
        }
        if (shader == null)
        {
            Debug.LogWarning("Material Fit could not find a mask shader. Falling back to the color buffer alpha.");
            return null;
        }
        Material material = new Material(shader);
        material.hideFlags = HideFlags.HideAndDontSave;
        if (material.HasProperty("_Color"))
        {
            material.SetColor("_Color", Color.white);
        }
        return material;
    }

    private static void NormalizeMaskTexture(Texture2D mask)
    {
        Color[] pixels = mask.GetPixels();
        for (int i = 0; i < pixels.Length; i++)
        {
            float value = Mathf.Clamp01((pixels[i].r + pixels[i].g + pixels[i].b) / 3.0f);
            pixels[i] = new Color(value, value, value, 1.0f);
        }
        mask.SetPixels(pixels);
        mask.Apply();
    }

    private static void ApplyAlphaMask(Texture2D texture, Texture2D mask)
    {
        Color[] pixels = texture.GetPixels();
        Color[] maskPixels = mask.GetPixels();
        int count = Mathf.Min(pixels.Length, maskPixels.Length);
        for (int i = 0; i < count; i++)
        {
            float alpha = Mathf.Clamp01((maskPixels[i].r + maskPixels[i].g + maskPixels[i].b) / 3.0f);
            Color pixel = pixels[i];
            pixel.a = alpha;
            pixels[i] = pixel;
        }
        texture.SetPixels(pixels);
        texture.Apply();
    }

    private static void ApplyBackgroundAlphaKey(Texture2D texture, Color keyColor, float tolerance, float softness)
    {
        Color[] pixels = texture.GetPixels();
        float safeSoftness = Mathf.Max(0.0001f, softness);
        for (int i = 0; i < pixels.Length; i++)
        {
            Color pixel = pixels[i];
            float distance = ColorDistanceRgb(pixel, keyColor);
            float alphaScale = Mathf.Clamp01((distance - tolerance) / safeSoftness);
            pixel.a *= alphaScale;
            pixels[i] = pixel;
        }
        texture.SetPixels(pixels);
        texture.Apply();
    }

    private static void WriteAlphaMaskPng(Texture2D source, string maskPath)
    {
        Color[] sourcePixels = source.GetPixels();
        Color[] maskPixels = new Color[sourcePixels.Length];
        for (int i = 0; i < sourcePixels.Length; i++)
        {
            float alpha = sourcePixels[i].a;
            maskPixels[i] = new Color(alpha, alpha, alpha, 1.0f);
        }

        Texture2D mask = new Texture2D(source.width, source.height, TextureFormat.RGBA32, false);
        mask.SetPixels(maskPixels);
        mask.Apply();
        File.WriteAllBytes(maskPath, mask.EncodeToPNG());
        DestroyImmediate(mask);
    }

    private static float ColorDistanceRgb(Color a, Color b)
    {
        float dr = a.r - b.r;
        float dg = a.g - b.g;
        float db = a.b - b.b;
        return Mathf.Sqrt(dr * dr + dg * dg + db * db);
    }

    private static List<float> ParseFloatList(string text)
    {
        List<float> values = new List<float>();
        if (string.IsNullOrEmpty(text))
        {
            return values;
        }

        string[] parts = text.Split(',');
        foreach (string rawPart in parts)
        {
            float value;
            if (float.TryParse(rawPart.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out value))
            {
                values.Add(value);
            }
        }
        return values;
    }

    private static bool TryGetRenderBounds(GameObject gameObject, out Bounds bounds)
    {
        Renderer[] renderers = gameObject.GetComponentsInChildren<Renderer>();
        if (renderers.Length == 0)
        {
            bounds = new Bounds(gameObject.transform.position, Vector3.one);
            return false;
        }

        bounds = renderers[0].bounds;
        for (int i = 1; i < renderers.Length; i++)
        {
            bounds.Encapsulate(renderers[i].bounds);
        }
        return true;
    }

    private static bool IsDescendantOf(Transform child, Transform ancestor)
    {
        Transform current = child != null ? child.parent : null;
        while (current != null)
        {
            if (current == ancestor)
            {
                return true;
            }
            current = current.parent;
        }
        return false;
    }

    private static string ResolveOutputPath(string path)
    {
        if (Path.IsPathRooted(path))
        {
            return path;
        }
        return Path.GetFullPath(Path.Combine(Directory.GetParent(Application.dataPath).FullName, path));
    }

    private static string GetTargetAssetPath(GameObject gameObject)
    {
        Object prefab = PrefabUtility.GetCorrespondingObjectFromSource(gameObject);
        if (prefab != null)
        {
            return AssetDatabase.GetAssetPath(prefab);
        }
        return AssetDatabase.GetAssetPath(gameObject);
    }

    private static string FormatAngle(float angle)
    {
        return angle.ToString("0.###", CultureInfo.InvariantCulture).Replace("-", "m").Replace(".", "p");
    }

    private static float[] ToArray(Vector3 value)
    {
        return new float[] { value.x, value.y, value.z };
    }

    [System.Serializable]
    private class CaptureMetadata
    {
        public string exporterVersion = string.Empty;
        public string exportedAtUtc = string.Empty;
        public string unityVersion = string.Empty;
        public string targetName = string.Empty;
        public string targetAssetPath = string.Empty;
        public string outputFolder = string.Empty;
        public int imageWidth = 0;
        public int imageHeight = 0;
        public bool transparentBackground = true;
        public bool useSilhouetteMaskAlpha = true;
        public bool keyBackgroundToAlpha = false;
        public bool exportMask = true;
        public float backgroundKeyTolerance = 0.0f;
        public float backgroundKeySoftness = 0.0f;
        public bool useCameraProjection = true;
        public string captureMode = string.Empty;
        public bool useOrthographic = false;
        public float fieldOfView = 0.0f;
        public float orthographicScale = 0.0f;
        public float orthographicSize = 0.0f;
        public float[] targetCenter = new float[3];
        public float[] targetSize = new float[3];
        public List<CaptureView> views = new List<CaptureView>();
    }

    [System.Serializable]
    private class CaptureView
    {
        public int index = 0;
        public float yaw = 0.0f;
        public float pitch = 0.0f;
        public string imagePath = string.Empty;
        public string fileName = string.Empty;
        public string maskPath = string.Empty;
        public string maskFileName = string.Empty;
        public float[] cameraPosition = new float[3];
        public float[] cameraRotationEuler = new float[3];
        public float[] targetLocalRotationEuler = new float[3];
    }

    private struct CameraState
    {
        public Vector3 position;
        public Quaternion rotation;
        public bool orthographic;
        public float orthographicSize;
        public float fieldOfView;
        public float nearClipPlane;
        public float farClipPlane;
        public CameraClearFlags clearFlags;
        public Color backgroundColor;
        public RenderTexture targetTexture;

        public static CameraState FromCamera(Camera camera)
        {
            return new CameraState
            {
                position = camera.transform.position,
                rotation = camera.transform.rotation,
                orthographic = camera.orthographic,
                orthographicSize = camera.orthographicSize,
                fieldOfView = camera.fieldOfView,
                nearClipPlane = camera.nearClipPlane,
                farClipPlane = camera.farClipPlane,
                clearFlags = camera.clearFlags,
                backgroundColor = camera.backgroundColor,
                targetTexture = camera.targetTexture
            };
        }

        public void ApplyTo(Camera camera)
        {
            camera.transform.position = position;
            camera.transform.rotation = rotation;
            camera.orthographic = orthographic;
            camera.orthographicSize = orthographicSize;
            camera.fieldOfView = fieldOfView;
            camera.nearClipPlane = nearClipPlane;
            camera.farClipPlane = farClipPlane;
            camera.clearFlags = clearFlags;
            camera.backgroundColor = backgroundColor;
            camera.targetTexture = targetTexture;
        }
    }

    private struct TransformState
    {
        public Vector3 localPosition;
        public Quaternion localRotation;
        public Vector3 localScale;

        public static TransformState FromTransform(Transform transform)
        {
            return new TransformState
            {
                localPosition = transform.localPosition,
                localRotation = transform.localRotation,
                localScale = transform.localScale
            };
        }

        public void ApplyTo(Transform transform)
        {
            transform.localPosition = localPosition;
            transform.localRotation = localRotation;
            transform.localScale = localScale;
        }
    }

    private struct RendererState
    {
        public Renderer renderer;
        public bool enabled;
        public Material[] sharedMaterials;

        public static RendererState FromRenderer(Renderer renderer)
        {
            return new RendererState
            {
                renderer = renderer,
                enabled = renderer.enabled,
                sharedMaterials = renderer.sharedMaterials
            };
        }

        public void Apply()
        {
            if (renderer == null)
            {
                return;
            }
            renderer.enabled = enabled;
            renderer.sharedMaterials = sharedMaterials;
        }
    }

}
#endif

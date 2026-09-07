using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CortenDesk.Remote.Services;

/// <summary>
/// Настройки и сохранённый токен в %APPDATA%\CortenDeskRemote\.
///
/// Токен лежит отдельным файлом и шифруется DPAPI на текущего пользователя
/// Windows: в settings.json его класть нельзя — это обычный читаемый JSON,
/// а токен клиентского API даёт доступ ко всему списку устройств.
/// </summary>
public sealed class AppSettings
{
    private const string FolderName = "CortenDeskRemote";
    private const string SettingsFile = "settings.json";
    private const string TokenFile = "token.dat";

    /// <summary>Энтропия DPAPI — привязывает шифртекст к этому приложению.</summary>
    private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("CortenDeskRemote.v1");

    [JsonPropertyName("serverUrl")]
    public string ServerUrl { get; set; } = "https://rd.spritelutsk.duckdns.org";

    [JsonPropertyName("username")]
    public string Username { get; set; } = "";

    [JsonPropertyName("rememberMe")]
    public bool RememberMe { get; set; } = true;

    /// <summary>Стабильный идентификатор установки — уходит в /api/login как id/uuid.</summary>
    [JsonPropertyName("deviceUuid")]
    public string DeviceUuid { get; set; } = "";

    [JsonPropertyName("autoRefreshSeconds")]
    public int AutoRefreshSeconds { get; set; } = 15;

    [JsonPropertyName("onlineOnly")]
    public bool OnlineOnly { get; set; }

    [JsonIgnore]
    public string DeviceId => "win-" + DeviceUuid;

    public static string Directory
    {
        get
        {
            var root = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            return Path.Combine(root, FolderName);
        }
    }

    public static AppSettings Load()
    {
        AppSettings settings;

        try
        {
            var path = Path.Combine(Directory, SettingsFile);
            settings = File.Exists(path)
                ? JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(path)) ?? new AppSettings()
                : new AppSettings();
        }
        catch
        {
            // Битый или недоступный файл настроек не должен мешать запуску.
            settings = new AppSettings();
        }

        if (string.IsNullOrWhiteSpace(settings.DeviceUuid))
            settings.DeviceUuid = Guid.NewGuid().ToString("N");

        if (settings.AutoRefreshSeconds < 5)
            settings.AutoRefreshSeconds = 15;

        return settings;
    }

    public void Save()
    {
        try
        {
            System.IO.Directory.CreateDirectory(Directory);
            var json = JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(Path.Combine(Directory, SettingsFile), json);
        }
        catch
        {
            // Не сохранилось — приложение всё равно работает, просто забудет выбор.
        }
    }

    public static void SaveToken(string token)
    {
        try
        {
            System.IO.Directory.CreateDirectory(Directory);
            var protectedBytes = ProtectedData.Protect(
                Encoding.UTF8.GetBytes(token), Entropy, DataProtectionScope.CurrentUser);
            File.WriteAllBytes(Path.Combine(Directory, TokenFile), protectedBytes);
        }
        catch
        {
        }
    }

    public static string? LoadToken()
    {
        try
        {
            var path = Path.Combine(Directory, TokenFile);
            if (!File.Exists(path)) return null;

            var plain = ProtectedData.Unprotect(
                File.ReadAllBytes(path), Entropy, DataProtectionScope.CurrentUser);

            return Encoding.UTF8.GetString(plain);
        }
        catch
        {
            // Файл перенесли с другой машины или профиль сменился — DPAPI не
            // расшифрует, и это нормально: просто попросим войти заново.
            return null;
        }
    }

    public static void ClearToken()
    {
        try
        {
            var path = Path.Combine(Directory, TokenFile);
            if (File.Exists(path)) File.Delete(path);
        }
        catch
        {
        }
    }
}

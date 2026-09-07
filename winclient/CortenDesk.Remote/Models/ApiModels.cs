using System.Text.Json.Serialization;

namespace CortenDesk.Remote.Models;

/// <summary>
/// Ответ POST /api/login. Контракт CortenDesk: ошибка приходит с HTTP 200 и
/// непустым полем <see cref="Error"/>, поэтому статус-код проверять мало.
/// </summary>
public sealed class LoginResponse
{
    [JsonPropertyName("access_token")] public string? AccessToken { get; set; }
    [JsonPropertyName("type")] public string? Type { get; set; }
    [JsonPropertyName("tfa_type")] public string? TfaType { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
    [JsonPropertyName("user")] public UserPayload? User { get; set; }
}

/// <summary>UserPayload из client-api §5 (тот же объект отдаёт /api/currentUser).</summary>
public sealed class UserPayload
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("display_name")] public string DisplayName { get; set; } = "";
    [JsonPropertyName("email")] public string Email { get; set; } = "";
    [JsonPropertyName("note")] public string Note { get; set; } = "";
    [JsonPropertyName("status")] public int Status { get; set; }
    [JsonPropertyName("is_admin")] public bool IsAdmin { get; set; }

    public string Title => string.IsNullOrWhiteSpace(DisplayName) ? Name : DisplayName;
}

/// <summary>Обёртка страничных ответов: {"total": N, "data": [...]}.</summary>
public sealed class PagedResponse<T>
{
    [JsonPropertyName("total")] public int Total { get; set; }
    [JsonPropertyName("data")] public List<T> Data { get; set; } = new();
    [JsonPropertyName("error")] public string? Error { get; set; }
}

/// <summary>PeerPayload из GET /api/peers (§20). `info` — всегда объект.</summary>
public sealed class Peer
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("info")] public PeerInfo Info { get; set; } = new();
    [JsonPropertyName("status")] public int Status { get; set; }
    [JsonPropertyName("user")] public string User { get; set; } = "";
    [JsonPropertyName("user_name")] public string UserName { get; set; } = "";
    [JsonPropertyName("device_group_name")] public string DeviceGroupName { get; set; } = "";
    [JsonPropertyName("note")] public string Note { get; set; } = "";
}

public sealed class PeerInfo
{
    [JsonPropertyName("username")] public string Username { get; set; } = "";
    [JsonPropertyName("os")] public string Os { get; set; } = "";
    [JsonPropertyName("device_name")] public string DeviceName { get; set; } = "";
}

/// <summary>Элемент GET /api/device-group/accessible (§18): читается только `name`.</summary>
public sealed class DeviceGroupName
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
}

using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using CortenDesk.Remote.Models;

namespace CortenDesk.Remote.Services;

/// <summary>Сервер ответил осмысленной ошибкой — текст показывается пользователю.</summary>
public class CortenDeskException : Exception
{
    public CortenDeskException(string message) : base(message) { }
}

/// <summary>Токен протух или отозван — нужно переспросить логин.</summary>
public sealed class AuthExpiredException : CortenDeskException
{
    public AuthExpiredException() : base("Сессия на сервере истекла, войдите заново.") { }
}

/// <summary>
/// Клиент REST API CortenDesk.
///
/// Два важных свойства контракта, из-за которых здесь не хватает обычного
/// EnsureSuccessStatusCode:
///   - ошибки приходят как {"error": "..."} даже с HTTP 200 (клиентский
///     протокол RustDesk так устроен, стоковый клиент статус не смотрит);
///   - списочные ответы страничные, {"total": N, "data": [...]}, максимум
///     pageSize=500, поэтому страницы докручиваются в цикле.
/// </summary>
public sealed class CortenDeskClient : IDisposable
{
    private const int PageSize = 200;
    private const int MaxPages = 200;      // предохранитель от бесконечного цикла

    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private readonly HttpClient _http;

    public CortenDeskClient(string baseUrl)
    {
        BaseUri = NormalizeBase(baseUrl);

        _http = new HttpClient
        {
            BaseAddress = BaseUri,
            Timeout = TimeSpan.FromSeconds(30),
        };
        _http.DefaultRequestHeaders.Add("User-Agent", "CortenDeskRemote/1.0 (Windows)");
    }

    public Uri BaseUri { get; }

    /// <summary>Bearer клиентского API. Пусто — значит не авторизованы.</summary>
    public string? Token { get; set; }

    /// <summary>Адрес встроенного веб-клиента для конкретного устройства.</summary>
    public Uri WebClientUri(string peerId) =>
        new(BaseUri, "/webclient?id=" + Uri.EscapeDataString(peerId));

    public Uri ConsoleUri => new(BaseUri, "/");

    /// <summary>Приводит введённый адрес к виду https://host[:port]/ .</summary>
    public static Uri NormalizeBase(string raw)
    {
        var trimmed = (raw ?? "").Trim();
        if (trimmed.Length == 0)
            throw new CortenDeskException("Укажите адрес сервера.");

        if (!trimmed.Contains("://", StringComparison.Ordinal))
            trimmed = "https://" + trimmed;

        if (!Uri.TryCreate(trimmed, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            throw new CortenDeskException($"Некорректный адрес сервера: {raw}");

        // По http логин и пароль ушли бы открытым текстом. Схему по умолчанию
        // мы и так подставляем https, но явный http надо отклонить — кроме
        // петли, где он нужен для отладки против локального сервера.
        if (uri.Scheme == Uri.UriSchemeHttp && !IsLoopback(uri.Host))
            throw new CortenDeskException(
                "Подключение по http небезопасно: логин и пароль уйдут открытым текстом. "
                + "Укажите адрес с https://");

        // Базовый адрес обязан оканчиваться слэшем, иначе относительные пути
        // затирают последний сегмент.
        var builder = new UriBuilder(uri) { Path = "/", Query = "", Fragment = "" };
        return builder.Uri;
    }

    private static bool IsLoopback(string host) =>
        host.Equals("localhost", StringComparison.OrdinalIgnoreCase)
        || host == "127.0.0.1"
        || host == "::1"
        || host == "[::1]";

    /// <summary>POST /api/login — выдаёт Bearer клиентского API.</summary>
    public async Task<UserPayload> LoginAsync(
        string username, string password, string deviceId, string deviceUuid, CancellationToken ct)
    {
        var body = new
        {
            username,
            password,
            id = deviceId,
            uuid = deviceUuid,
            autoLogin = false,
            type = "account",
            verificationCode = "",
            deviceInfo = new
            {
                os = "Windows",
                type = "client",
                name = Environment.MachineName,
            },
        };

        using var response = await _http.PostAsJsonAsync("api/login", body, Json, ct)
            .ConfigureAwait(false);

        var payload = await ReadAsync<LoginResponse>(response, ct).ConfigureAwait(false);

        if (!string.IsNullOrEmpty(payload.Error))
            throw new CortenDeskException(Translate(payload.Error));

        if (!string.IsNullOrEmpty(payload.TfaType))
            throw new CortenDeskException(
                "Для этой учётной записи включена двухфакторная аутентификация, "
                + "клиентский API её не поддерживает. Войдите через веб-консоль.");

        if (string.IsNullOrEmpty(payload.AccessToken))
            throw new CortenDeskException("Сервер не вернул токен доступа.");

        Token = payload.AccessToken;
        return payload.User ?? new UserPayload { Name = username };
    }

    /// <summary>POST /api/currentUser — проверка живости сохранённого токена.</summary>
    public async Task<UserPayload> CurrentUserAsync(CancellationToken ct)
    {
        using var request = Authorized(HttpMethod.Post, "api/currentUser");
        using var response = await _http.SendAsync(request, ct).ConfigureAwait(false);
        return await ReadAsync<UserPayload>(response, ct).ConfigureAwait(false);
    }

    /// <summary>GET /api/peers — устройства, видимые текущему пользователю.</summary>
    public Task<List<Peer>> GetPeersAsync(CancellationToken ct) =>
        GetAllPagesAsync<Peer>("api/peers", ct);

    /// <summary>GET /api/device-group/accessible — доступные папки устройств.</summary>
    public async Task<List<string>> GetDeviceGroupsAsync(CancellationToken ct)
    {
        var groups = await GetAllPagesAsync<DeviceGroupName>("api/device-group/accessible", ct)
            .ConfigureAwait(false);

        return groups
            .Select(g => g.Name)
            .Where(n => !string.IsNullOrWhiteSpace(n))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(n => n, StringComparer.CurrentCultureIgnoreCase)
            .ToList();
    }

    /// <summary>POST /api/logout — отзывает токен на сервере, ошибки игнорируются.</summary>
    public async Task LogoutAsync(CancellationToken ct)
    {
        if (string.IsNullOrEmpty(Token)) return;

        try
        {
            using var request = Authorized(HttpMethod.Post, "api/logout");
            using var response = await _http.SendAsync(request, ct).ConfigureAwait(false);
        }
        catch
        {
            // Выход из приложения не должен падать из-за недоступного сервера.
        }
        finally
        {
            Token = null;
        }
    }

    private async Task<List<T>> GetAllPagesAsync<T>(string path, CancellationToken ct)
    {
        var all = new List<T>();

        for (var page = 1; page <= MaxPages; page++)
        {
            var url = $"{path}?current={page}&pageSize={PageSize}";

            using var request = Authorized(HttpMethod.Get, url);
            using var response = await _http.SendAsync(request, ct).ConfigureAwait(false);

            var payload = await ReadAsync<PagedResponse<T>>(response, ct).ConfigureAwait(false);

            if (!string.IsNullOrEmpty(payload.Error))
                throw new CortenDeskException(Translate(payload.Error));

            all.AddRange(payload.Data);

            // Страница пришла неполной или мы уже собрали total — дальше пусто.
            if (payload.Data.Count < PageSize || all.Count >= payload.Total)
                break;
        }

        return all;
    }

    private HttpRequestMessage Authorized(HttpMethod method, string url)
    {
        if (string.IsNullOrEmpty(Token))
            throw new AuthExpiredException();

        var request = new HttpRequestMessage(method, url);
        request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", Token);
        return request;
    }

    private static async Task<T> ReadAsync<T>(HttpResponseMessage response, CancellationToken ct)
    {
        if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
            throw new AuthExpiredException();

        if (response.StatusCode == HttpStatusCode.TooManyRequests)
            throw new CortenDeskException("Слишком много запросов к серверу, попробуйте через минуту.");

        var text = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);

        if (string.IsNullOrWhiteSpace(text))
        {
            if (!response.IsSuccessStatusCode)
                throw new CortenDeskException($"Сервер ответил {(int)response.StatusCode}.");

            throw new CortenDeskException("Сервер вернул пустой ответ.");
        }

        T? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<T>(text, Json);
        }
        catch (JsonException)
        {
            // Типичный случай: вместо JSON прилетела HTML-страница логина или
            // ошибка прокси — показывать её сырой бессмысленно.
            throw new CortenDeskException(
                response.IsSuccessStatusCode
                    ? "Сервер вернул не JSON. Проверьте адрес: он должен указывать на консоль CortenDesk."
                    : $"Сервер ответил {(int)response.StatusCode} и вернул не JSON.");
        }

        if (parsed is null)
            throw new CortenDeskException("Сервер вернул пустой ответ.");

        return parsed;
    }

    /// <summary>Сообщения клиентского API приходят по-английски и коротко.</summary>
    private static string Translate(string error) => error switch
    {
        "Wrong credentials" => "Неверный логин или пароль.",
        _ => error,
    };

    public void Dispose() => _http.Dispose();
}

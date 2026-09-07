namespace CortenDesk.Remote.Models;

/// <summary>
/// Строка таблицы устройств. Отдельный тип, а не сам <see cref="Peer"/>:
/// сетка биндится на плоские строковые свойства, а исходный payload
/// сохраняется целиком на случай, если понадобится ещё поле.
/// </summary>
public sealed class PeerRow
{
    public PeerRow(Peer peer)
    {
        Source = peer;
        Id = peer.Id;
        DeviceName = string.IsNullOrWhiteSpace(peer.Info.DeviceName) ? peer.Id : peer.Info.DeviceName;
        Os = peer.Info.Os;
        LocalUser = peer.Info.Username;
        Owner = string.IsNullOrWhiteSpace(peer.UserName) ? peer.User : peer.UserName;
        Group = peer.DeviceGroupName;
        Note = peer.Note;
        IsOnline = peer.Status == 1;
    }

    public Peer Source { get; }
    public string Id { get; }
    public string DeviceName { get; }
    public string Os { get; }
    public string LocalUser { get; }
    public string Owner { get; }
    public string Group { get; }
    public string Note { get; }
    public bool IsOnline { get; }

    public string StatusText => IsOnline ? "В сети" : "Не в сети";

    /// <summary>Поля, по которым бьёт строка поиска.</summary>
    public bool Matches(string needle)
    {
        if (string.IsNullOrWhiteSpace(needle)) return true;

        return Contains(Id, needle)
            || Contains(DeviceName, needle)
            || Contains(Os, needle)
            || Contains(LocalUser, needle)
            || Contains(Owner, needle)
            || Contains(Group, needle)
            || Contains(Note, needle);
    }

    private static bool Contains(string haystack, string needle) =>
        haystack.Contains(needle, StringComparison.OrdinalIgnoreCase);
}

import Foundation

public enum Greeting {
  public static func message(for name: String) -> String {
    let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
    return "Hello, \(trimmed.isEmpty ? "friend" : trimmed)!"
  }
}

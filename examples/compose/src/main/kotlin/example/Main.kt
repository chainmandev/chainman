package example

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material.Button
import androidx.compose.material.MaterialTheme
import androidx.compose.material.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application

fun greeting(name: String): String {
    val trimmed = name.trim()
    return "Hello, ${if (trimmed.isEmpty()) "friend" else trimmed}!"
}

fun main() =
    application {
        Window(onCloseRequest = ::exitApplication, title = "Greeting") {
            MaterialTheme {
                var clicks by remember { mutableStateOf(0) }
                Column(Modifier.padding(24.dp)) {
                    Text(greeting("Compose"))
                    Text("Clicks: $clicks")
                    Button(onClick = { clicks += 1 }) {
                        Text("Count")
                    }
                }
            }
        }
    }

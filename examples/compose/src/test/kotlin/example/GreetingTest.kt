package example

import kotlin.test.Test
import kotlin.test.assertEquals

class GreetingTest {
    @Test
    fun blankNameUsesFallback() {
        assertEquals("Hello, friend!", greeting("  "))
    }

    @Test
    fun providedNameIsTrimmed() {
        assertEquals("Hello, Ada!", greeting(" Ada "))
    }
}

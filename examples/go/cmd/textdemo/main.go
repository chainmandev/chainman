package main

import (
	"fmt"
	"os"

	"example.test/textcore"
)

func main() {
	fmt.Println(textcore.Summarize(os.Args[1:]))
}
